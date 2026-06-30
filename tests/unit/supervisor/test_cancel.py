"""Unit tests for ``RunsSupervisor.cancel`` paths.

Covers graceful SIGTERM on a live run, already-done short-circuit,
the disk-state orphan fallback (run not in memory but state file
exists with dead pid), invalid mode validation, and unknown-run
error mapping.

Uses a fake child subprocess (``python -c "import time; time.sleep(N)"``)
to exercise signal handling without a real orcho pipeline.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orcho_mcp.errors import RunNotFoundError
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


@pytest.mark.asyncio
async def test_cancel_signal_sent_for_live_run(fake_workspace):
    runs_dir = fake_workspace / "runspace" / "runs"
    run_dir = runs_dir / "live_run"
    run_dir.mkdir()

    # Spawn a long-sleep child we can SIGTERM cleanly.
    proc = _spawn_fake_child(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        run_dir,
    )

    sup = RunsSupervisor()
    handle = RunHandle(
        run_id="live_run",
        pid=proc.pid,
        pgid=proc.pid,
        run_dir=run_dir,
        project_dir=str(run_dir),
        command=["fake"],
        started_at="t",
        popen=proc,
    )
    write_state(handle)
    sup._runs["live_run"] = handle

    try:
        result = await sup.cancel("live_run", mode="graceful")
        assert result["status"] == "signal_sent(graceful)"
        # Wait for the child to die (it should — SIGTERM on plain Python sleeper).
        for _ in range(50):
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.asyncio
async def test_cancel_already_done(fake_workspace):
    runs_dir = fake_workspace / "runspace" / "runs"
    run_dir = runs_dir / "done_run"
    run_dir.mkdir()

    proc = _spawn_fake_child([sys.executable, "-c", "pass"], run_dir)
    proc.wait()

    sup = RunsSupervisor()
    handle = RunHandle(
        run_id="done_run",
        pid=proc.pid,
        pgid=proc.pid,
        run_dir=run_dir,
        project_dir=str(run_dir),
        command=["fake"],
        started_at="t",
        popen=proc,
    )
    sup._runs["done_run"] = handle

    result = await sup.cancel("done_run", mode="graceful")
    assert result["status"] == "already_done"


@pytest.mark.asyncio
async def test_cancel_invalid_mode_raises():
    sup = RunsSupervisor()
    with pytest.raises(ValueError):
        await sup.cancel("any", mode="explode")


@pytest.mark.asyncio
async def test_cancel_unknown_run_raises_via_state_path(fake_workspace):
    sup = RunsSupervisor()
    with pytest.raises(RunNotFoundError):
        await sup.cancel("nope_does_not_exist", mode="graceful")


@pytest.mark.asyncio
async def test_cancel_orphan_via_disk_state(fake_workspace):
    runs_dir = fake_workspace / "runspace" / "runs"
    run_dir = runs_dir / "orphan_run"
    run_dir.mkdir()

    # State file says pid 99999999 (dead), supervisor not tracking in memory.
    (run_dir / "mcp_supervisor.json").write_text(json.dumps({
        "run_id":  "orphan_run",
        "pid":     99999999,
        "pgid":    99999999,
        "command": ["x"],
        "cwd":     "/p",
        "project_dir": "/p",
        "started_at": "t",
        "status":  "running",
    }))

    sup = RunsSupervisor()
    result = await sup.cancel("orphan_run", mode="graceful")
    assert result["status"] == "already_dead"
