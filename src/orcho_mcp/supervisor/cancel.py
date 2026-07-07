"""orcho_mcp.supervisor.cancel — ``execute`` for graceful / hard cancel.

Delegates signal delivery to ``sdk.run_control.cancel_run`` — the single
home for the ``killpg`` mechanics — while keeping the MCP-side ordering
invariant and a layered, deterministic state-file contract:

- **Layered state.** MCP owns ``mcp_supervisor.json`` (the delta
  ``recover`` reads); the SDK owns ``run_supervisor.json`` (the pid / pgid
  source ``cancel_run`` reads, written by ``launch_run`` / ``resume_run``
  at spawn / respawn).
- **Owned run** (a live handle in ``sup._runs``): the MCP order is
  preserved — terminal ``meta.json`` → ``already_done``, then
  ``Popen.poll()`` → ``already_done``, only THEN delegate the signal.
  ``cancel_run`` does not consult the live ``Popen``, so that poll check
  stays MCP-side.
- **Re-attached orphan** (no in-memory handle): a *deterministic* bridge,
  not a choice. Read ``mcp_supervisor.json``; if absent → ``RunNotFoundError``
  (prior behaviour). If ``run_supervisor.json`` is missing (a run started
  before this refactor, or one whose neutral state was never written),
  materialise a compatible one from the MCP fields BEFORE delegating, so
  ``cancel_run`` can drive it without re-introducing a private ``killpg``.
- **Settle mirroring.** When ``cancel_run`` settles a run (``already_dead``)
  the settled status is mirrored back into ``mcp_supervisor.json`` so a
  later ``recover()`` never re-sees a stale ``running``.

Composed into ``RunsSupervisor`` via a thin delegation method in
``manager.py``; this module exports the operation as a top-level
function that takes the supervisor as its first argument.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sdk.errors import RunNotFound as SdkRunNotFound
from sdk.run_control.launch import (
    LaunchedRun,
    cancel_run,
    read_launch_state,
    write_launch_state,
)

from orcho_mcp.errors import RunNotFoundError
from orcho_mcp.supervisor.paths import resolve_runs_dir
from orcho_mcp.supervisor.state import (
    STATE_FILE,
    meta_status_is_terminal,
    read_state,
    write_state,
)

if TYPE_CHECKING:
    from pathlib import Path

    from orcho_mcp.supervisor.manager import RunsSupervisor


def _materialise_launch_state(run_dir: Path, state: dict) -> None:
    """Bridge ``mcp_supervisor.json`` → ``run_supervisor.json``.

    ``cancel_run`` is ``run_supervisor.json``-driven (the neutral SDK state
    ``launch_run`` writes at spawn). A run re-attached across a supervisor
    restart — or one started before this delegation refactor — may carry
    only the MCP ``mcp_supervisor.json``. Materialise a compatible
    ``run_supervisor.json`` from those fields so the delegated cancel has
    the pid / pgid it needs, without re-adding a private ``killpg`` here.
    """
    pid = int(state.get("pid", 0))
    run = LaunchedRun(
        run_id=state.get("run_id", run_dir.name),
        pid=pid,
        pgid=int(state.get("pgid", pid)),
        run_dir=run_dir,
        project_dir=state.get("project_dir") or state.get("cwd") or "",
        command=list(state.get("command", [])),
        started_at=state.get("started_at", ""),
        mock=bool(state.get("mock", False)),
        output_mode=state.get("output_mode", "summary"),
        status=state.get("status", "running"),
    )
    write_launch_state(run)


def _mirror_settled_orphan_state(run_dir: Path, state: dict) -> None:
    """Reflect a delegated settle back into ``mcp_supervisor.json``.

    ``recover`` reads ``mcp_supervisor.json``; after ``cancel_run`` reports
    a dead pid we overwrite the MCP delta with a settled status so a later
    ``recover()`` never re-sees a stale ``running`` for this run.
    """
    state["status"] = "interrupted"
    if not state.get("halt_reason"):
        state["halt_reason"] = "interrupted_orphan"
    (run_dir / STATE_FILE).write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


async def execute(
    sup: RunsSupervisor, run_id: str, mode: str = "graceful",
) -> dict[str, str]:
    """Send SIGTERM (graceful) or SIGKILL (hard) to the run's process group.

    Works for both spawned-this-lifetime runs (owned handle) and
    re-attached orphans whose state lives on disk but whose ``Popen``
    handle was lost on supervisor restart. Signal delivery is delegated to
    ``sdk.run_control.cancel_run``.
    """
    if mode not in ("graceful", "hard"):
        raise ValueError(f"cancel mode must be 'graceful' or 'hard', got {mode!r}")

    handle = sup._runs.get(run_id)

    if handle is None:
        # ── Orphan path: no in-memory handle. ────────────────────────────
        runs_dir = resolve_runs_dir()
        run_dir = runs_dir / run_id
        state = read_state(run_dir)
        if state is None:
            raise RunNotFoundError(f"run {run_id}: no state file")
        # Deterministic bridge: cancel_run reads run_supervisor.json; if the
        # neutral state was never written, materialise it from the MCP delta
        # before delegating so cancel works for any orphan (incl. pre-refactor).
        if read_launch_state(run_dir) is None:
            _materialise_launch_state(run_dir, state)
        try:
            result = cancel_run(run_id, runs_dir=str(runs_dir), mode=mode)
        except SdkRunNotFound as e:
            raise RunNotFoundError(str(e)) from e
        # Mirror a settle back into mcp_supervisor.json for recover().
        if result.status == "already_dead":
            _mirror_settled_orphan_state(run_dir, state)
        return {"run_id": run_id, "status": result.status}

    # ── Owned run: live handle (usually with a Popen). ───────────────────
    #
    # MCP ordering invariant, pipeline truth first: if ``meta.json:status``
    # reports a terminal status the run is finished even if the OS hasn't
    # finalised the subprocess yet. ``Popen.poll()`` would still return
    # ``None`` in that window (``_reap()`` hasn't ``wait()``-ed), so without
    # these checks cancel would race and signal a just-exited process.
    # ``cancel_run`` does not consult the live ``Popen``, so the poll check
    # stays MCP-side. ``awaiting_phase_handoff`` is intentionally NOT
    # terminal (paused, not finished) — see ``META_TERMINAL_STATUSES``.
    if meta_status_is_terminal(handle.run_dir):
        return {"run_id": run_id, "status": "already_done"}
    if handle.popen is not None and handle.popen.poll() is not None:
        return {"run_id": run_id, "status": "already_done"}

    # Owned runs carry a run_supervisor.json (written by launch_run /
    # resume_run at spawn), so no bridge is needed here; delegate the signal.
    runs_dir = handle.run_dir.parent
    try:
        result = cancel_run(run_id, runs_dir=str(runs_dir), mode=mode)
    except SdkRunNotFound as e:
        raise RunNotFoundError(str(e)) from e
    if result.status == "already_dead":
        handle.status = "interrupted"
        if handle.halt_reason is None:
            handle.halt_reason = "interrupted_orphan"
        write_state(handle)
    return {"run_id": run_id, "status": result.status}


__all__ = ["execute"]
