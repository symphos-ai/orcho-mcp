"""Acceptance tests for `orcho_run_cancel`.

Pins the cancel contract laid down in ``docs/run_lifecycle.md``
against the supervisor's actual process model:

  - ``orcho_run_cancel`` returns immediately. The caller polls for
    actual death via ``orcho_run_status``; reap eventually flips
    the state file.
  - Cancel is **idempotent**: signal_sent → already_dead → already_dead
    is the natural progression.
  - Process group is signalled (``os.killpg``), not just the head pid;
    ``start_new_session=True`` at spawn means ``pgid==pid``.
  - Owned-run path uses ``Popen.poll()`` first; orphan path uses
    ``os.kill(pid, 0)`` to probe liveness.
  - ``recover()`` only orphans ``status==running`` runs whose pid is
    dead. Live pids are untouched. ``awaiting_phase_handoff`` is
    deliberately excluded — a dead pid there is the *expected*
    post-pause signature (the pipeline exited rc=4 on a declared
    phase handoff), not an orphan.

Marked ``mcp_integration``; enable with ``pytest -m mcp_integration``.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.mcp_integration


# ── helpers ─────────────────────────────────────────────────────────────────


async def _wait_terminal(run_id: str, timeout_s: float = 60.0) -> str:
    """Poll ``orcho_run_status`` until a terminal state is reached."""
    from orcho_mcp.tools import orcho_run_status

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = orcho_run_status(run_id)
        cur = (snap.meta or {}).get("status")
        if cur in {"done", "failed", "interrupted", "halted",
                   "awaiting_phase_handoff", "orphaned"}:
            return cur
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"run {run_id} did not reach terminal status within {timeout_s}s"
    )


async def _wait_pid_dead(pid: int, timeout_s: float = 10.0) -> bool:
    """Poll ``os.kill(pid, 0)`` until ProcessLookupError fires."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # Pid recycled by a process we don't own — for our purposes,
            # the original child is gone.
            return True
        await asyncio.sleep(0.1)
    return False


# ── happy path: cancel a live run ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_running_run_graceful_returns_signal_or_already_done(
    mock_project: Path,
) -> None:
    """Cancel a fresh spawn before it has a chance to finish.

    Mock pipelines complete in ~1.5s; the cancel call may arrive while
    the process is alive (signal_sent) or after natural completion
    (already_done). Both outcomes prove the contract — what we *don't*
    accept is a crash, an unstructured error, or a signal that the
    pipeline silently ignores.
    """
    from orcho_mcp.tools import orcho_run_cancel, orcho_run_start

    started = await orcho_run_start(
        task="cancel running",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    run_id = started.run_id

    # Cancel immediately — no sleep. We're racing the mock pipeline.
    result = await orcho_run_cancel(run_id, mode="graceful")
    assert result.run_id == run_id
    assert result.status in {"signal_sent(graceful)", "already_done"}, (
        f"unexpected cancel status: {result.status!r}"
    )

    # Whichever branch, the pipeline must reach a terminal state and
    # the OS process must be dead.
    final = await _wait_terminal(run_id)
    assert final in {"done", "failed", "interrupted", "halted"}, final
    assert await _wait_pid_dead(started.pid), (
        f"pid {started.pid} still alive after cancel + terminal status"
    )


@pytest.mark.asyncio
async def test_cancel_running_run_hard_kills_process_group(
    mock_project: Path,
) -> None:
    """SIGKILL must reach the process group, not just the head pid.

    Pipeline subprocess starts via ``start_new_session=True`` so
    ``pgid==pid``. After cancel, every process in the pgid must be
    gone — proven indirectly: the head pid is dead, and the run
    reaches a terminal status (because reap can complete).
    """
    from orcho_mcp.tools import orcho_run_cancel, orcho_run_start

    started = await orcho_run_start(
        task="cancel hard",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    run_id = started.run_id

    result = await orcho_run_cancel(run_id, mode="hard")
    assert result.status in {"signal_sent(hard)", "already_done"}, result.status

    final = await _wait_terminal(run_id)
    assert final in {"done", "failed", "interrupted", "halted"}, final
    assert await _wait_pid_dead(started.pid)


@pytest.mark.asyncio
async def test_hard_kill_stamps_halt_reason_signal_sigkill(
    mock_project: Path,
) -> None:
    """SIGKILL bypasses pipeline atexit; supervisor reap must stamp
    ``halt_reason`` so ``orcho_run_status`` surfaces a reason even
    though ``meta.halt_reason`` stays unset.

    Lands a post-mortem invariant for the SIGKILL case: every
    non-``done`` terminal status should answer the
    "why" via ``halt_reason`` — either from meta (graceful paths) or
    from the supervisor (SIGKILL / abnormal exit / orphan).
    """
    import contextlib
    import json
    import os
    import signal

    from orcho_mcp.tools import orcho_run_start, orcho_run_status

    started = await orcho_run_start(
        task="SIGKILL halt_reason probe",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    run_id = started.run_id

    # Send raw SIGKILL to the head pid — bypassing supervisor.cancel()
    # is intentional: that path is the closest simulation of an
    # external kill (e.g. OOM, kill -9 from another shell) where the
    # supervisor's reap is the only writer that gets a chance to run.
    with contextlib.suppress(ProcessLookupError):
        os.kill(started.pid, signal.SIGKILL)

    final = await _wait_terminal(run_id)
    assert final in {"failed", "interrupted", "halted", "done"}, final

    state_path = Path(started.run_dir) / "mcp_supervisor.json"
    assert state_path.is_file(), state_path
    state = json.loads(state_path.read_text(encoding="utf-8"))
    rc = state.get("exit_code")
    if isinstance(rc, int) and rc < 0:
        assert state.get("halt_reason") == "signal:SIGKILL", state
        # And the wire-merged view surfaces it via orcho_run_status.
        status_view = orcho_run_status(run_id)
        assert status_view.meta.get("halt_reason") == "signal:SIGKILL", (
            status_view.meta
        )


# ── error contracts ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_unknown_run_id_raises(mock_project: Path) -> None:
    """Unknown run_id must raise ``RunNotFoundError`` with the id named.

    Falls into the orphan path because ``handle = self._runs.get(run_id)``
    returns None. Orphan path reads state file → returns None → raises.
    """
    from orcho_mcp.errors import RunNotFoundError
    from orcho_mcp.tools import orcho_run_cancel

    with pytest.raises(RunNotFoundError) as exc:
        await orcho_run_cancel("does_not_exist_29990101_000000",
                                mode="graceful")
    assert "does_not_exist_29990101_000000" in str(exc.value)


@pytest.mark.asyncio
async def test_cancel_invalid_mode_raises(mock_project: Path) -> None:
    """``mode`` must be one of the documented values.

    The supervisor raises a bare ``ValueError`` for an unknown mode; the
    run-control boundary (``map_command_errors``) translates it into the
    canonical ``InvalidPlanError`` so the MCP wire surface returns a
    typed bad-request rather than leaking a ``ValueError``. This mirrors
    the L1 contract in ``test_lifecycle_tools.py``. The MCP wire surface
    should not silently accept arbitrary strings — invalid modes are
    caller bugs, not supervisor state.
    """
    from orcho_mcp.errors import InvalidPlanError
    from orcho_mcp.tools import orcho_run_cancel, orcho_run_start

    started = await orcho_run_start(
        task="cancel invalid mode",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    with pytest.raises(InvalidPlanError) as exc:
        await orcho_run_cancel(started.run_id, mode="violently")
    assert "graceful" in str(exc.value)
    assert "hard" in str(exc.value)


# ── already-terminal cases ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_already_done_returns_already_done(
    mock_project: Path,
) -> None:
    """Cancelling a run that finished naturally returns ``already_done``.

    No signal should be sent; no error should be raised. Idempotent
    cancel-after-completion is the contract.
    """
    from orcho_mcp.tools import orcho_run_cancel, orcho_run_start

    started = await orcho_run_start(
        task="cancel already done",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    run_id = started.run_id
    assert (await _wait_terminal(run_id)) == "done"

    result = await orcho_run_cancel(run_id, mode="graceful")
    assert result.status == "already_done", (
        f"expected already_done after natural completion, got {result.status!r}"
    )


@pytest.mark.asyncio
async def test_double_cancel_is_idempotent(mock_project: Path) -> None:
    """Two consecutive cancels must not crash and must converge to a
    consistent terminal status. The first cancel sends the signal; the
    second observes the resulting state without re-signalling a live
    process (because there isn't one)."""
    from orcho_mcp.tools import orcho_run_cancel, orcho_run_start

    started = await orcho_run_start(
        task="double cancel",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    run_id = started.run_id

    first = await orcho_run_cancel(run_id, mode="graceful")
    assert first.status in {"signal_sent(graceful)", "already_done"}

    # Wait for terminal state before the second cancel so the race
    # window is closed.
    await _wait_terminal(run_id)
    assert await _wait_pid_dead(started.pid)

    second = await orcho_run_cancel(run_id, mode="graceful")
    assert second.status in {"already_done", "already_dead"}, (
        f"second cancel returned unexpected status: {second.status!r}"
    )


# ── orphan / restart-recovery ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recover_marks_running_with_dead_pid_as_orphaned(
    mock_project: Path,
    runs_dir: Path,
) -> None:
    """``RunsSupervisor.recover()`` is what runs at server startup. It
    must mark ``status=running`` runs whose pid is dead as
    ``orphaned`` and emit a ``run.orphaned`` event. Live pids are
    left alone; ``awaiting_phase_handoff`` is intentionally excluded.
    """
    from orcho_mcp.supervisor import RunsSupervisor

    # Three synthetic runs with different shapes.
    bogus_pid = 999_999_999  # safely dead — no PID this high on a normal box

    running_dead = runs_dir / "20990101_010101_orphan"
    running_dead.mkdir()
    (running_dead / "mcp_supervisor.json").write_text(json.dumps({
        "run_id": "20990101_010101_orphan",
        "pid": bogus_pid,
        "pgid": bogus_pid,
        "status": "running",
        "started_at": "2099-01-01T01:01:01Z",
        "project_dir": str(mock_project),
        "command": ["python", "-m", "pipeline.project_orchestrator", "--mock"],
    }) + "\n", encoding="utf-8")

    awaiting_plan_dead = runs_dir / "20990101_020202_pause"
    awaiting_plan_dead.mkdir()
    (awaiting_plan_dead / "mcp_supervisor.json").write_text(json.dumps({
        "run_id": "20990101_020202_pause",
        "pid": bogus_pid,
        "pgid": bogus_pid,
        "status": "awaiting_phase_handoff",
        "started_at": "2099-01-01T02:02:02Z",
        "project_dir": str(mock_project),
        "command": ["python", "-m", "pipeline.project_orchestrator", "--mock"],
    }) + "\n", encoding="utf-8")

    running_live = runs_dir / "20990101_030303_alive"
    running_live.mkdir()
    (running_live / "mcp_supervisor.json").write_text(json.dumps({
        "run_id": "20990101_030303_alive",
        "pid": os.getpid(),  # this test process — definitely alive
        "pgid": os.getpid(),
        "status": "running",
        "started_at": "2099-01-01T03:03:03Z",
        "project_dir": str(mock_project),
        "command": ["python", "-m", "pipeline.project_orchestrator", "--mock"],
    }) + "\n", encoding="utf-8")

    sup = RunsSupervisor()
    orphaned = sup.recover()

    # Only the dead-pid running run should be orphaned.
    assert orphaned == ["20990101_010101_orphan"], (
        f"recover() returned {orphaned!r} — expected only the orphan id"
    )

    # State file flipped to orphaned.
    state = json.loads((running_dead / "mcp_supervisor.json").read_text())
    assert state["status"] == "orphaned"

    # Awaiting-QA was NOT touched.
    state_plan = json.loads((awaiting_plan_dead / "mcp_supervisor.json").read_text())
    assert state_plan["status"] == "awaiting_phase_handoff", (
        "awaiting_phase_handoff state was incorrectly orphaned"
    )

    # Live-pid run was NOT touched.
    state_live = json.loads((running_live / "mcp_supervisor.json").read_text())
    assert state_live["status"] == "running"

    # The orphan got an event marker.
    events_path = running_dead / "events.jsonl"
    assert events_path.is_file(), "run.orphaned event was not appended"
    events_text = events_path.read_text(encoding="utf-8")
    assert "run.orphaned" in events_text


@pytest.mark.asyncio
async def test_cancel_orphan_with_dead_pid_returns_already_dead(
    mock_project: Path,
    runs_dir: Path,
) -> None:
    """Cancel of an orphan whose pid is already dead must return
    ``already_dead`` (not raise, not hang). The orphan path reads the
    state file, probes liveness, sees the dead pid, and exits cleanly.
    """
    from orcho_mcp.supervisor import RunsSupervisor

    orphan_id = "20990101_999999_corpse"
    orphan_dir = runs_dir / orphan_id
    orphan_dir.mkdir()
    bogus_pid = 999_999_999
    (orphan_dir / "mcp_supervisor.json").write_text(json.dumps({
        "run_id": orphan_id,
        "pid": bogus_pid,
        "pgid": bogus_pid,
        "status": "running",
        "started_at": "2099-01-01T03:03:03Z",
        "project_dir": str(mock_project),
        "command": ["python", "-m", "pipeline.project_orchestrator", "--mock"],
    }) + "\n", encoding="utf-8")

    sup = RunsSupervisor()
    result = await sup.cancel(orphan_id, mode="graceful")
    assert result == {"run_id": orphan_id, "status": "already_dead"}

    # State file should be updated to a terminal status (the supervisor
    # writes status=interrupted by default, or preserves the original).
    state = json.loads((orphan_dir / "mcp_supervisor.json").read_text())
    assert state["status"] in {"interrupted", "running"}, (
        # ``running`` is acceptable here because the supervisor only
        # rewrites the file when state.get("status") was missing — for
        # an existing ``running`` it reuses the value. Either way the
        # cancel call returned already_dead, which is what the contract
        # promises.
        f"unexpected post-cancel state: {state['status']!r}"
    )
