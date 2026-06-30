"""orcho_mcp.services.run_lookup — SDK adapters for runs-dir / run-dir resolution.

Single entry point for MCP tools and resources that need to resolve the
workspace runs directory or a specific run directory. Translates the
SDK's exception vocabulary (``sdk.NoWorkspace`` / ``sdk.RunNotFound``)
into the MCP wire-format errors clients expect.

Walk-up is **disabled** (``cwd=None``) by design: MCP runs as a
long-lived server typically started from an arbitrary cwd by an IDE or
service manager. cwd-based walk-up would silently bind to whichever
directory the user happened to launch from. The server must use the
explicit ``$ORCHO_WORKSPACE`` / ``$ORCHO_WORKTREE`` env vars or fail
loudly.
"""
from __future__ import annotations

from pathlib import Path

from core.infra import config as _core_config
from sdk import (
    NoWorkspace as _SDKNoWorkspace,
    RunNotFound as _SDKRunNotFound,
    find_run,
    find_runs_dir,
)

from orcho_mcp.errors import (
    RunNotFoundError,
    WorkspaceNotResolvedError,
)


def runs_dir_or_raise() -> Path:
    """Resolve the runs directory or raise ``WorkspaceNotResolvedError``.

    Translates ``sdk.NoWorkspace`` into the MCP wire-format error the
    clients already expect.
    """
    try:
        return find_runs_dir(cwd=None)
    except _SDKNoWorkspace as e:
        raise WorkspaceNotResolvedError(
            f"could not resolve runs directory: {e}. "
            "Set $ORCHO_WORKSPACE or run the MCP server from inside an "
            "orcho workspace."
        ) from e


def workspace_root_or_none() -> Path | None:
    """Resolve the workspace root directory, or ``None`` when unresolved.

    Defensive sibling of :func:`runs_dir_or_raise` for callers that must
    *not* fail when no workspace is configured — it mirrors the
    ``get_workspace_info`` pattern in ``services.read_queries`` (resolve via
    ``core.infra.config.get_workspace_dir``; swallow any failure to ``None``).
    Used by the pending-decisions classifier to decide whether a run's
    project path lies under the workspace; a ``None`` result simply disables
    the workspace-relative classification rules rather than raising.
    """
    try:
        return _core_config.get_workspace_dir()
    except Exception:  # noqa: BLE001 - defensive: unresolved workspace → None
        return None


def find_run_dir(run_id: str) -> Path:
    """Return the run directory for ``run_id`` or raise ``RunNotFoundError``.

    Thin wrapper over ``sdk.find_run`` (walk-up disabled — see
    ``runs_dir_or_raise``) for the MCP error contract.
    """
    try:
        ref = find_run(run_id, cwd=None)
    except _SDKRunNotFound as e:
        raise RunNotFoundError(f"run not found: {run_id}") from e
    except _SDKNoWorkspace as e:
        raise WorkspaceNotResolvedError(str(e)) from e
    return ref.run_dir


__all__ = ["find_run_dir", "runs_dir_or_raise", "workspace_root_or_none"]
