"""Acceptance smoke for MCP tool-handler run start.

The L4 ``test_orcho_run_mock.py`` already proves ``RunsSupervisor``
works against a real ``--mock`` subprocess. This sibling test closes
the remaining gap: it goes through the *MCP tool function*
(``orcho_mcp.tools.orcho_run_start``) — same call path the live bridge
hits — instead of importing ``RunsSupervisor`` directly. If
the supervisor package or SDK argv-builder wiring ever fails at
tool-handler invocation time, this test catches it in CI rather than
waiting for the next live MCP restart.

Marked ``mcp_integration`` so the default suite stays fast; runs in
under a second on a warm cache.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.mcp_integration


@pytest.fixture
def mock_project(tmp_path: Path, monkeypatch) -> Path:
    # Project dir must be a real git repo — orcho-core's worktree
    # resolver hard-fails on non-git ``project_dir`` (``3b516ec``).
    from tests.conftest import init_git_repo

    ws = tmp_path / "ws"
    project = ws / "demo_project"
    runs_dir = ws / "runspace" / "runs"
    project.mkdir(parents=True)
    runs_dir.mkdir(parents=True)
    (project / "README.md").write_text("# Demo project\n", encoding="utf-8")
    init_git_repo(project)
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    return project


@pytest.mark.asyncio
async def test_orcho_run_via_tool_handler_then_read_round_trip(
    mock_project: Path,
) -> None:
    """spawn via tool → status → metrics → history all SDK-routed."""
    from orcho_mcp.tools import (
        orcho_run_history,
        orcho_run_metrics,
        orcho_run_start,
        orcho_run_status,
    )

    started = await orcho_run_start(
        task="tool-handler smoke",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    run_id = started.run_id
    assert started.pid > 0
    assert started.run_dir.endswith(run_id)

    # Poll status until the pipeline reaches a terminal state.
    deadline = time.monotonic() + 60
    final_status: str | None = None
    while time.monotonic() < deadline:
        snap = orcho_run_status(run_id)
        cur = (snap.meta or {}).get("status")
        if cur in ("done", "failed", "interrupted", "halted"):
            final_status = cur
            break
        await asyncio.sleep(0.3)

    assert final_status == "done", f"unexpected final status: {final_status}"

    # Summary-only contract: the default status payload elides heavy
    # phase bodies. ``implement.output`` (the agent's final text) must
    # not ship inline by default, while ``include=["all"]`` restores it.
    done = orcho_run_status(run_id)
    impl = (done.meta or {}).get("phases", {}).get("implement")
    if isinstance(impl, dict):
        assert "output" not in impl, "implement body leaked into summary payload"
        assert "output_chars" in impl, "summary marker missing for implement body"
    full = orcho_run_status(run_id, include=["all"])
    full_impl = (full.meta or {}).get("phases", {}).get("implement")
    if isinstance(full_impl, dict):
        assert "output" in full_impl, "include=['all'] should restore the body"

    # Read tools must round-trip the run we just spawned.
    metrics = orcho_run_metrics(run_id).metrics
    assert metrics, "metrics.json should be populated after a done run"
    assert "phases" in metrics, "phases breakdown missing from metrics"

    hist = orcho_run_history(limit=5)
    assert hist.runs, "history is empty after spawn"
    assert hist.runs[0].run_id == run_id, (
        f"newest history row is {hist.runs[0].run_id!r}, expected {run_id!r}"
    )
    assert hist.runs[0].status == "done"
