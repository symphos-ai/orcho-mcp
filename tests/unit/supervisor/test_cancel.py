"""Unit tests for ``RunsSupervisor.cancel`` paths.

Cancel delegates signal delivery to ``sdk.run_control.cancel_run`` while
keeping the MCP ordering invariant (terminal ``meta.json`` →
``Popen.poll()`` → signal) and a layered, deterministic state-file
contract (MCP owns ``mcp_supervisor.json``; the SDK owns
``run_supervisor.json``).

Covers: graceful SIGTERM on a live owned run (both state files present, no
bridge), the already-done short-circuit (poll wins before delegation), the
re-attached-orphan disk-state path (only ``mcp_supervisor.json`` present →
the cancel path materialises ``run_supervisor.json`` and mirrors the
settle back), invalid-mode validation, and unknown-run error mapping.

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
from tests.fixtures.mcp_workspace import launch_state


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
    # Owned run: the neutral run_supervisor.json exists too (written by
    # launch_run at spawn). Materialise the already-consistent companion so
    # the delegated cancel_run finds the live pid without the orphan bridge.
    (run_dir / "run_supervisor.json").write_text(
        json.dumps(launch_state(
            run_id="live_run", pid=proc.pid, pgid=proc.pid, status="running",
        ))
    )
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

    # ``Popen.poll()`` sees the exited child and short-circuits to
    # already_done BEFORE any delegation — no run_supervisor.json needed.
    result = await sup.cancel("done_run", mode="graceful")
    assert result["status"] == "already_done"


@pytest.mark.asyncio
async def test_cancel_already_done_via_terminal_meta(fake_workspace):
    """Terminal ``meta.json`` wins over a still-``None`` ``Popen.poll()``.

    Pins the MCP ordering invariant: pipeline truth (meta terminal) is
    checked first, so a live-looking Popen in the post-flush window does
    not race a SIGTERM into a just-exited process.
    """
    runs_dir = fake_workspace / "runspace" / "runs"
    run_dir = runs_dir / "meta_done_run"
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"status": "done"}))

    # A still-alive sleeper so poll() would be None — meta must win first.
    proc = _spawn_fake_child(
        [sys.executable, "-c", "import time; time.sleep(10)"], run_dir,
    )

    sup = RunsSupervisor()
    handle = RunHandle(
        run_id="meta_done_run",
        pid=proc.pid,
        pgid=proc.pid,
        run_dir=run_dir,
        project_dir=str(run_dir),
        command=["fake"],
        started_at="t",
        popen=proc,
    )
    sup._runs["meta_done_run"] = handle

    try:
        result = await sup.cancel("meta_done_run", mode="graceful")
        assert result["status"] == "already_done"
        # The live child was NOT signalled (meta short-circuit fired first).
        assert proc.poll() is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


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

    # ONLY mcp_supervisor.json is present (a re-attached orphan / pre-refactor
    # run): pid 99999999 (dead), status running, supervisor not tracking it in
    # memory. This is the bridge test — the cancel path must materialise a
    # compatible run_supervisor.json before delegating to cancel_run.
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

    # The bridge materialised the neutral state file for the delegation.
    assert (run_dir / "run_supervisor.json").is_file(), (
        "cancel must materialise run_supervisor.json from mcp_supervisor.json"
    )

    # And the settle is mirrored back into mcp_supervisor.json so a later
    # recover() never re-sees a stale 'running'.
    mcp_state = json.loads((run_dir / "mcp_supervisor.json").read_text())
    assert mcp_state["status"] != "running", (
        "delegated cancel must settle the MCP state, not leave it running"
    )
    assert mcp_state["status"] == "interrupted"
    assert mcp_state["halt_reason"] == "interrupted_orphan"
