"""Acceptance tests for real pipeline subprocesses via ``--mock``.

End-to-end through the supervisor: spawn a real ``pipeline.project_orchestrator``
subprocess in mock mode, then assert the run reached ``done`` status with
checkpoint, events, and supervisor state files all populated.

Mock mode is fully hermetic in orcho-core 0.3.1+ — every phase agent slot
is replaced with an inline stub via ``make_mock_phase_config`` so zero
real ``claude`` / ``codex`` / ``gemini`` CLI calls fire. Test runs in
under a second on a warm cache; safe to enable in CI without provider
binaries installed.

Marked ``mcp_integration`` so the default test run skips it; opt-in with
``pytest -m mcp_integration``.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

pytestmark = pytest.mark.mcp_integration


@pytest.fixture
def mock_project(tmp_path, monkeypatch):
    """Create a minimal orcho-trackable project + workspace.

    Project dir must be a real git repo — orcho-core's worktree
    resolver hard-fails on non-git ``project_dir`` (``3b516ec``).
    """
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
async def test_orcho_run_mock_full_lifecycle(mock_project):
    """Real pipeline subprocess via mock provider; assert clean completion.

    Verifies the supervisor's spawn → reap path actually works against a
    real pipeline process (not just a fake-child sleep). Catches breakages
    in the ``ORCHO_RUN_ID`` contract, build_orch_argv emission, and
    subprocess group lifecycle that pure unit tests miss.
    """
    from orcho_mcp.supervisor import RunsSupervisor

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="trivial mock task — say hello",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )

    # run_id format: ts_HHMMSS_xxxxxx
    assert len(handle.run_id.split("_")) == 3
    assert handle.run_dir.is_dir()
    assert handle.pid > 0

    # mcp_supervisor.json written immediately at spawn (before reap).
    state_path = handle.run_dir / "mcp_supervisor.json"
    assert state_path.is_file()
    state = json.loads(state_path.read_text())
    assert state["run_id"] == handle.run_id
    assert state["pid"] == handle.pid
    assert state["status"] == "running"

    # Wait for pipeline to finish — clean exit OR mock-mode failure (both
    # acceptable for this test; we're verifying supervisor contract, not
    # pipeline-side mock completeness).
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if handle.popen is not None and handle.popen.poll() is not None:
            break
        await asyncio.sleep(0.5)
    else:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.kill()
        pytest.fail("mock pipeline didn't finish within 90s")

    # Let the reap task update state.
    for _ in range(50):
        if handle.status != "running":
            break
        await asyncio.sleep(0.1)

    # ── Strong assertions — pipeline must complete cleanly under mock mode.
    # Mock is hermetic in orcho-core 0.3.1 (see make_mock_phase_config),
    # so any non-zero exit indicates a real bug in supervisor or pipeline,
    # not provider-CLI absence.

    assert handle.exit_code == 0, (
        f"mock pipeline exited rc={handle.exit_code}; "
        f"runner.log tail: "
        f"{(handle.run_dir / 'runner.log').read_text()[-2000:] if (handle.run_dir / 'runner.log').is_file() else '(no log)'}"
    )
    assert handle.status == "done"

    final_state = json.loads(state_path.read_text())
    assert final_state["status"] == "done"
    assert final_state["exit_code"] == 0

    # Pipeline writes meta.json — supervisor never touches it (pipeline's
    # contract). Run reached at least the run.start phase + run.end on
    # clean completion.
    meta_path = handle.run_dir / "meta.json"
    assert meta_path.is_file()

    events_path = handle.run_dir / "events.jsonl"
    assert events_path.is_file()
    events_lines = [
        json.loads(line) for line in events_path.read_text().splitlines() if line.strip()
    ]
    kinds = {e["kind"] for e in events_lines}
    assert "run.start" in kinds
    assert "run.end" in kinds, f"expected clean run.end; events seen: {kinds}"
    # Phase markers appeared — at minimum plan + plan_qa under default profile.
    assert any(k.startswith("phase.") for k in kinds), kinds


@pytest.mark.asyncio
async def test_meta_worktree_block_surfaces_through_mcp(mock_project):
    """``meta.worktree`` is written by every run
    and round-trips through ``orcho_run_status`` as part of
    ``RunStatus.meta``.

    The mock project here is a plain directory (no ``.git``), so the
    worktree resolver degrades to ``mode='off'`` with a populated
    ``degraded_reason``. That degraded shape is itself part of the
    public wire — operators need to know why isolation didn't apply.

    Pins per CLAUDE.md "MCP per-phase validation" rule: every
    wire-format-touching phase in orcho-core ships with a matching
    orcho-mcp E2E mock smoke.
    """
    from orcho_mcp.supervisor import RunsSupervisor
    from orcho_mcp.tools import orcho_run_status

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="smoke for meta.worktree wire shape",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if handle.popen is not None and handle.popen.poll() is not None:
            break
        await asyncio.sleep(0.5)
    else:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.kill()
        pytest.fail("mock pipeline didn't finish within 90s")
    for _ in range(50):
        if handle.status != "running":
            break
        await asyncio.sleep(0.1)

    snap = orcho_run_status(handle.run_id)
    worktree = snap.meta.get("worktree")
    assert worktree is not None, (
        f"meta.worktree missing from RunStatus.meta; meta keys: "
        f"{list(snap.meta.keys())}"
    )
    # Required worktree fields:
    assert "isolation" in worktree
    assert worktree["isolation"] in {"off", "per_run"}, (
        f"unexpected isolation value: {worktree['isolation']!r}"
    )
    assert "path" in worktree
    assert "base_ref" in worktree
    assert "branch_ref" in worktree
    # The mock_project fixture is now a real git repo (orcho-core
    # ``3b516ec`` hard-fails worktree isolation on non-git project_dir),
    # so isolation is the active ``per_run`` mode — NOT the legacy
    # degraded ``off`` fallback (that path was removed). A successful
    # isolated run carries no degraded_reason.
    assert worktree["isolation"] == "per_run", (
        f"unexpected isolation value: {worktree['isolation']!r}"
    )
    assert not worktree.get("degraded_reason"), (
        "an isolated git-repo run should not populate degraded_reason"
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"
