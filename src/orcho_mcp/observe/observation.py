"""orcho_mcp.observe.observation — advisory workspace-state observation writer.

Best-effort wrapper that takes a ``RunEventsSummary`` snapshot and
records the cursor (``last_seq`` / ``last_status`` / ``last_phase``)
into ``<ORCHO_WORKSPACE>/mcp/state.json`` via the existing
``orcho_mcp.workspace_state`` storage primitive. The state file is a
reconnect hint, not the source of truth — every write swallows its own
exceptions and logs at debug.

Called from ``build_run_events_summary`` (which ``watch_run`` routes
through on every poll, so the watch path picks up updates for free).
"""
from __future__ import annotations

import logging
from pathlib import Path

from core.infra import config as _core_config

from orcho_mcp.schemas import RunEventsSummary
from orcho_mcp.workspace_state import update_run_state

logger = logging.getLogger(__name__)


def workspace_dir_or_none() -> Path | None:
    """Resolve the workspace root for advisory state writes.

    Same resolution path ``orcho_workspace_info`` uses
    (``_core_config.get_workspace_dir()``). Returns ``None`` instead of
    raising — callers are best-effort write paths that must keep tool
    behaviour usable when the workspace cannot be resolved (e.g. server
    started outside any orcho-tracked project). The corresponding read
    path is the public ``orcho_workspace_state`` tool, which raises
    ``WorkspaceNotResolvedError`` like every other read tool.
    """
    try:
        return _core_config.get_workspace_dir()
    except Exception:  # noqa: BLE001
        return None


def record_workspace_observation(
    run_id: str, snap: RunEventsSummary,
) -> None:
    """Best-effort advisory write of one observation to the state file.

    Wraps ``workspace_state.update_run_state`` in a broad exception
    swallow so a corrupt / read-only / locked state file cannot break
    the calling read tool. The state file is a reconnect hint, not the
    source of truth — log at debug and move on.

    Called from ``build_run_events_summary`` (which ``watch_run``
    routes through on every poll, so the watch path picks up updates
    for free).
    """
    workspace_dir = workspace_dir_or_none()
    if workspace_dir is None:
        return
    try:
        update_run_state(
            workspace_dir,
            run_id=run_id,
            last_seq=snap.next_seq,
            last_status=snap.status,
            last_phase=snap.current_phase,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "failed to update advisory workspace MCP state for run %s",
            run_id,
            exc_info=True,
        )


__all__ = ["record_workspace_observation", "workspace_dir_or_none"]
