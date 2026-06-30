"""orcho_mcp.supervisor.cancel — ``execute`` for graceful / hard cancel.

Sends SIGTERM (``graceful``) or SIGKILL (``hard``) to the run's process
group. Works for both spawned-this-lifetime runs (via ``Popen``) and
re-attached orphans whose state lives on disk but whose ``Popen``
handle was lost on supervisor restart.

Composed into ``RunsSupervisor`` via a thin delegation method in
``manager.py``; this module exports the operation as a top-level
function that takes the supervisor as its first argument.
"""
from __future__ import annotations

import json
import os
import signal
from typing import TYPE_CHECKING

from orcho_mcp.errors import RunNotFoundError
from orcho_mcp.supervisor.paths import resolve_runs_dir
from orcho_mcp.supervisor.process import is_pid_alive
from orcho_mcp.supervisor.state import (
    STATE_FILE,
    meta_status_is_terminal,
    read_state,
    write_state,
)

if TYPE_CHECKING:
    from orcho_mcp.supervisor.manager import RunsSupervisor


async def execute(
    sup: RunsSupervisor, run_id: str, mode: str = "graceful",
) -> dict[str, str]:
    """Send SIGTERM (graceful) or SIGKILL (hard) to the run's process group.

    Works for both spawned-this-lifetime runs (via Popen) and re-attached
    orphans (via raw ``os.kill``).
    """
    if mode not in ("graceful", "hard"):
        raise ValueError(f"cancel mode must be 'graceful' or 'hard', got {mode!r}")

    sig = signal.SIGTERM if mode == "graceful" else signal.SIGKILL

    handle = sup._runs.get(run_id)
    if handle is None:
        # Try restart-recovery path: read state, use raw pid.
        runs_dir = resolve_runs_dir()
        run_dir = runs_dir / run_id
        state = read_state(run_dir)
        if state is None:
            raise RunNotFoundError(f"run {run_id}: no state file")
        pid = int(state.get("pid", 0))
        pgid = int(state.get("pgid", pid))
        if not is_pid_alive(pid):
            state["status"] = state.get("status", "interrupted")
            if not state.get("halt_reason"):
                state["halt_reason"] = "interrupted_orphan"
            (run_dir / STATE_FILE).write_text(
                json.dumps(state, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            return {"run_id": run_id, "status": "already_dead"}
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return {"run_id": run_id, "status": "already_dead"}
        return {"run_id": run_id, "status": f"signal_sent({mode})"}

    # Owned run — has Popen.
    #
    # Pipeline truth first: if ``meta.json:status`` reports a
    # terminal status (done / failed / halted / interrupted /
    # orphaned), the run is finished even if the OS hasn't yet
    # finalised the subprocess. ``Popen.poll()`` would still return
    # ``None`` in that window because ``_reap()`` hasn't called
    # ``wait()`` yet — without this check, cancel would race and
    # send SIGTERM into a process that has just exited.
    # ``awaiting_phase_handoff`` is intentionally NOT in the
    # terminal set (it is paused, not finished); see
    # ``META_TERMINAL_STATUSES`` in ``supervisor.state``.
    if meta_status_is_terminal(handle.run_dir):
        return {"run_id": run_id, "status": "already_done"}
    if handle.popen is not None and handle.popen.poll() is not None:
        return {"run_id": run_id, "status": "already_done"}

    try:
        os.killpg(handle.pgid, sig)
    except ProcessLookupError:
        handle.status = "interrupted"
        if handle.halt_reason is None:
            handle.halt_reason = "interrupted_orphan"
        write_state(handle)
        return {"run_id": run_id, "status": "already_dead"}

    return {"run_id": run_id, "status": f"signal_sent({mode})"}


__all__ = ["execute"]
