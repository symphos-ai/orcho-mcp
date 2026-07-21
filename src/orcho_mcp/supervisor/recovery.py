"""orcho_mcp.supervisor.recovery — ``recover`` startup probe.

Scans the runs directory for stale ``mcp_supervisor.json`` files and
marks abandoned in-flight runs as ``orphaned`` (status flip +
``run.orphaned`` event). Paused-handoff runs (rc=4 →
``awaiting_phase_handoff``) MUST stay paused even when their pid is
dead — the dead pid is the expected post-pause signature, not an
orphan condition. Live pids of any state are left alone; without a
``Popen`` handle we can't reap properly via ``wait()`` so we don't
re-attach.

Composed into ``RunsSupervisor`` via a thin delegation method in
``manager.py``; this module exports the operation as a top-level
function that takes the supervisor as its first argument.
"""
from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from core.observability.events import append_event

from orcho_mcp.errors import WorkspaceNotResolvedError
from orcho_mcp.supervisor.handle import RunHandle
from orcho_mcp.supervisor.paths import resolve_runs_dir
from orcho_mcp.supervisor.process import is_pid_alive
from orcho_mcp.supervisor.state import read_state, settle_launch

if TYPE_CHECKING:
    from orcho_mcp.supervisor.manager import RunsSupervisor


def recover(sup: RunsSupervisor) -> list[str]:
    """Scan ``runs_dir`` for stale ``mcp_supervisor.json`` files.

    Only ``running`` runs whose pid is no longer alive are orphaned —
    ``awaiting_phase_handoff`` is intentionally NOT included even
    though the file shows the pid is dead. That state means the
    pipeline exited rc=4 on purpose (a phase's declared handoff
    policy paused the run); the dead pid is the expected post-pause
    signature, not an orphan condition. The run waits for
    ``orcho_phase_handoff_decide`` to be called and resumed.

    Live pids of any state are left alone — without a Popen handle we
    can't reap properly via ``wait()`` so we don't try to re-attach.

    Returns the list of orphaned run_ids. The ``sup`` argument is
    accepted for signature symmetry with the other operation modules
    but not consumed: recovery operates on disk state only, no
    in-memory supervisor mutation.
    """
    del sup  # signature symmetry; no in-memory state touched
    try:
        runs_dir = resolve_runs_dir()
    except WorkspaceNotResolvedError:
        return []
    if not runs_dir.is_dir():
        return []

    orphaned: list[str] = []
    for entry in runs_dir.iterdir():
        if not entry.is_dir():
            continue
        state = read_state(entry)
        if state is None:
            continue
        status = state.get("status")
        # Only ``running`` is a candidate for orphaning. Paused QA state
        # is expected to have a dead pid — it's not an orphan, it's
        # waiting for human/automated approval.
        if status != "running":
            continue
        pid = int(state.get("pid", 0))
        if is_pid_alive(pid):
            # Still running under some other supervisor instance — leave alone.
            continue

        handle = RunHandle(
            run_id=entry.name, pid=pid, pgid=int(state.get("pgid", pid)),
            run_dir=entry, project_dir=str(state.get("project_dir") or state.get("cwd") or ""),
            command=list(state.get("command") or []), started_at=str(state.get("started_at") or ""),
            mock=bool(state.get("mock", False)), output_mode=str(state.get("output_mode") or "full"),
            status="orphaned", halt_reason=str(state.get("halt_reason") or "orphaned_no_supervisor"),
        )
        if not settle_launch(handle):
            continue
        with contextlib.suppress(Exception):
            append_event(
                entry,
                "run.orphaned",
                {
                    "pid": pid,
                    "previous_status": status,
                    "reason": "mcp_server_restart_no_live_pid",
                },
            )
        orphaned.append(entry.name)
    return orphaned


__all__ = ["recover"]
