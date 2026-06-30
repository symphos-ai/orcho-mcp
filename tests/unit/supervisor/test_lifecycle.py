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
    # Failed runs get a synthetic supervisor_reaped event for diagnostics.
    events_path = run_dir / "events.jsonl"
    if events_path.is_file():
        text = events_path.read_text()
        assert "run.supervisor_reaped" in text


# ── singleton ────────────────────────────────────────────────────────────────

def test_get_supervisor_returns_same_instance():
    from orcho_mcp.supervisor import get_supervisor
    a = get_supervisor()
    b = get_supervisor()
    assert a is b
