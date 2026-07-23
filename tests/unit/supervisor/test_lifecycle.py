"""Unit tests for supervisor reap + singleton.

Catch-all home for supervisor concerns that don't fit the more
specific files in this directory:

- reap behaviour: rc=0 → done; rc=4 → awaiting_phase_handoff;
  other rc → failed (plus a synthetic ``run.supervisor_reaped``
  event for diagnostics).
- ``get_supervisor`` returns a shared singleton across calls.

Uses a fake child subprocess to drive reap deterministically.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from sdk.run_control import LaunchedRun, read_launch_state, write_launch_state

from orcho_mcp.supervisor import RunHandle, RunsSupervisor
from orcho_mcp.supervisor.state import write_state


def _spawn_fake_child(cmd: list[str], cwd: Path) -> subprocess.Popen:
    """Spawn a controllable child for direct (non-supervisor) tests."""
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── reap behaviour ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reap_done_for_rc_zero(fake_workspace):
    run_dir = fake_workspace / "runspace" / "runs" / "reap_clean"
    run_dir.mkdir()

    proc = _spawn_fake_child([sys.executable, "-c", "pass"], run_dir)
    sup = RunsSupervisor()
    handle = RunHandle(
        run_id="reap_clean",
        pid=proc.pid,
        pgid=proc.pid,
        run_dir=run_dir,
        project_dir=str(run_dir),
        command=["fake"],
        started_at="t",
        popen=proc,
    )
    write_state(handle)
    await sup._reap(handle)

    assert handle.exit_code == 0
    assert handle.status == "done"
    state = json.loads((run_dir / "mcp_supervisor.json").read_text())
    assert state["status"] == "done"
    assert read_launch_state(run_dir)["status"] == "done"


@pytest.mark.asyncio
async def test_reap_awaiting_plan_for_rc_4(fake_workspace):
    run_dir = fake_workspace / "runspace" / "runs" / "reap_qa"
    run_dir.mkdir()

    proc = _spawn_fake_child([sys.executable, "-c", "import sys; sys.exit(4)"], run_dir)
    sup = RunsSupervisor()
    handle = RunHandle(
        run_id="reap_qa",
        pid=proc.pid,
        pgid=proc.pid,
        run_dir=run_dir,
        project_dir=str(run_dir),
        command=["fake"],
        started_at="t",
        popen=proc,
    )
    write_state(handle)
    await sup._reap(handle)

    assert handle.exit_code == 4
    assert handle.status == "awaiting_phase_handoff"
    assert read_launch_state(run_dir)["status"] == "awaiting_phase_handoff"


@pytest.mark.asyncio
async def test_reap_failed_for_other_rc(fake_workspace):
    run_dir = fake_workspace / "runspace" / "runs" / "reap_fail"
    run_dir.mkdir()

    proc = _spawn_fake_child([sys.executable, "-c", "import sys; sys.exit(2)"], run_dir)
    sup = RunsSupervisor()
    handle = RunHandle(
        run_id="reap_fail",
        pid=proc.pid,
        pgid=proc.pid,
        run_dir=run_dir,
        project_dir=str(run_dir),
        command=["fake"],
        started_at="t",
        popen=proc,
    )
    write_state(handle)
    await sup._reap(handle)

    assert handle.exit_code == 2
    assert handle.status == "failed"
    assert read_launch_state(run_dir)["status"] == "failed"
    # Failed runs get a synthetic supervisor_reaped event for diagnostics.
    events_path = run_dir / "events.jsonl"
    if events_path.is_file():
        text = events_path.read_text()
        assert "run.supervisor_reaped" in text


@pytest.mark.asyncio
async def test_reap_signal_is_interrupted(fake_workspace):
    run_dir = fake_workspace / "runspace" / "runs" / "reap_signal"
    run_dir.mkdir()
    proc = _spawn_fake_child(
        [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
        run_dir,
    )
    handle = RunHandle("reap_signal", proc.pid, proc.pid, run_dir, str(run_dir), ["fake"], "t", popen=proc)
    write_state(handle)

    await RunsSupervisor()._reap(handle)

    assert handle.status == "interrupted"
    assert handle.halt_reason == "signal:SIGTERM"
    assert read_launch_state(run_dir)["status"] == "interrupted"


@pytest.mark.asyncio
async def test_stale_reap_does_not_overwrite_newer_pid(fake_workspace):
    run_dir = fake_workspace / "runspace" / "runs" / "reap_stale"
    run_dir.mkdir()
    proc = _spawn_fake_child([sys.executable, "-c", "pass"], run_dir)
    handle = RunHandle("reap_stale", proc.pid, proc.pid, run_dir, str(run_dir), ["fake"], "t", popen=proc)
    write_state(handle)
    newer = {**json.loads((run_dir / "mcp_supervisor.json").read_text()), "pid": proc.pid + 1, "status": "running"}
    (run_dir / "mcp_supervisor.json").write_text(json.dumps(newer), encoding="utf-8")

    await RunsSupervisor()._reap(handle)

    assert json.loads((run_dir / "mcp_supervisor.json").read_text())["pid"] == proc.pid + 1


@pytest.mark.asyncio
async def test_stale_reap_does_not_overwrite_newer_core_supervisor_pid(fake_workspace):
    """A CLI/core resume can replace only run_supervisor.json between reaps."""
    run_dir = fake_workspace / "runspace" / "runs" / "reap_core_stale"
    run_dir.mkdir()
    proc = _spawn_fake_child([sys.executable, "-c", "pass"], run_dir)
    handle = RunHandle("reap_core_stale", proc.pid, proc.pid, run_dir, str(run_dir), ["fake"], "t", popen=proc)
    write_state(handle)
    write_launch_state(LaunchedRun(
        run_id=handle.run_id, pid=proc.pid + 1, pgid=proc.pid + 1,
        run_dir=run_dir, project_dir=str(run_dir), command=["new-resume"],
        started_at="new", mock=False, output_mode="summary", status="running",
    ))

    await RunsSupervisor()._reap(handle)

    assert json.loads((run_dir / "mcp_supervisor.json").read_text())["pid"] == proc.pid
    assert read_launch_state(run_dir)["pid"] == proc.pid + 1


# ── singleton ────────────────────────────────────────────────────────────────

def test_get_supervisor_returns_same_instance():
    from orcho_mcp.supervisor import get_supervisor
    a = get_supervisor()
    b = get_supervisor()
    assert a is b
