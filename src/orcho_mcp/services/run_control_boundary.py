"""orcho_mcp.services.run_control_boundary — durable run controllability.

Classifies whether *this* MCP server can mutate (resume / decide) a run, or
whether the run is inspect-only from MCP's point of view.

The single durable signal is ``<run_dir>/mcp_supervisor.json``: a run is
``mcp_controllable`` exactly when that supervisor state file is present **and**
carries a resolvable ``project_dir`` (or its ``cwd`` fallback) — the two facts
``supervisor.resume.execute`` itself requires to respawn the pipeline (see
``supervisor/resume.py``: ``state.get("project_dir") or state.get("cwd")``).
Anything else — a CLI-started or otherwise foreign run dir that only carries
``meta.json``, or a state file missing the project dir — is ``inspect_only``.

The classification is deliberately on-disk only. It must NOT consult the
volatile ``supervisor._runs`` registry: after a server restart an MCP-started
run keeps its durable ``mcp_supervisor.json`` but is absent from the in-memory
map, and reading the volatile state would falsely demote it to inspect-only.

Pure module: no SDK calls beyond run-dir resolution, no log parsing. The
``mcp_supervisor.json`` read reuses ``status_merge._read_supervisor_state`` so
there is one canonical reader for the MCP-private state file (never an import
of the ``supervisor`` package).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from orcho_mcp.services.run_lookup import find_run_dir
from orcho_mcp.services.status_merge import _read_supervisor_state

Control = Literal["mcp_controllable", "inspect_only"]


@dataclass(frozen=True)
class RunControlProjection:
    """Durable controllability verdict for a single run.

    Attributes:
        control: ``mcp_controllable`` when this MCP server can resume/decide
            the run, ``inspect_only`` otherwise.
        reason: One-line human-readable fact behind the verdict.
        has_supervisor_state: Whether ``mcp_supervisor.json`` was readable.
        project_dir: The resolved project dir (``project_dir`` or ``cwd`` from
            the state file), or ``None`` when unresolvable.
    """

    control: Control
    reason: str
    has_supervisor_state: bool
    project_dir: str | None


def project_run_control(run_id: str) -> RunControlProjection:
    """Classify ``run_id`` as ``mcp_controllable`` or ``inspect_only``.

    Resolves the run directory (raising ``RunNotFoundError`` for a genuinely
    missing run), then reads ``mcp_supervisor.json`` durable state. The run is
    ``mcp_controllable`` only when the state file is present *and* yields a
    non-empty ``project_dir`` (or ``cwd`` fallback) — the exact durable facts
    ``supervisor.resume.execute`` needs to respawn the pipeline.
    """
    run_dir = find_run_dir(run_id)
    state = _read_supervisor_state(run_dir)
    if state is None:
        return RunControlProjection(
            control="inspect_only",
            reason=(
                "no mcp_supervisor.json — run not started by this MCP "
                "server, inspect-only"
            ),
            has_supervisor_state=False,
            project_dir=None,
        )
    project_dir = state.get("project_dir") or state.get("cwd")
    if not project_dir:
        return RunControlProjection(
            control="inspect_only",
            reason=(
                "mcp_supervisor.json present but no resolvable project_dir — "
                "inspect-only"
            ),
            has_supervisor_state=True,
            project_dir=None,
        )
    return RunControlProjection(
        control="mcp_controllable",
        reason=f"mcp_supervisor.json present; project_dir={project_dir}",
        has_supervisor_state=True,
        project_dir=project_dir,
    )


__all__ = ["Control", "RunControlProjection", "project_run_control"]
