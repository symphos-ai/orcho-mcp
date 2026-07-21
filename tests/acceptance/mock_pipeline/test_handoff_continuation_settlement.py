"""Coordinated MCP/core handoff settlement regression.

This intentionally compact L4 trace proves the operator-visible contract over
the real mock subprocess: a UTF-8 retry decision is idempotent, reads stop
offering decide immediately, and the resumed launch settles both supervisor
artifacts after the client has stopped watching it.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests._core_source import pin_core_source
from tests.fixtures.mcp_workspace import init_git_repo, supervisor_state, write_run

_CORE_CHECKOUT = pin_core_source()
pytestmark = pytest.mark.mcp_integration


async def _wait(run_id: str, wanted: set[str], timeout_s: float = 45.0) -> str:
    from orcho_mcp.tools import orcho_run_status

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = (orcho_run_status(run_id).meta or {}).get("status")
        if status in wanted:
            return status
        await asyncio.sleep(0.2)
    raise AssertionError(f"{run_id} did not reach {wanted}")


@pytest.mark.asyncio
async def test_retry_replay_resume_and_settlement_readback(
    mock_project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate rejection → UTF-8 retry replay → same-run resume → settled readback."""
    from orcho_mcp.tools import (
        orcho_phase_handoff_decide,
        orcho_run_diagnose,
        orcho_run_events_summary,
        orcho_run_evidence,
        orcho_run_live_status,
        orcho_run_resume,
        orcho_run_start,
        orcho_run_status,
        orcho_run_watch,
        orcho_workspace_pending_decisions,
    )

    # This core-supported trigger produces a deterministic pause.  Clear it
    # before the resumed subprocess so retry_feedback can complete its retry.
    monkeypatch.setenv("ORCHO_MOCK_IMPLEMENT_INCOMPLETE", "1")
    started = await orcho_run_start(
        task="coordinated gate:pytest-unit:1 retry trace",
        project_dir=str(mock_project), profile="feature", mock=True,
        max_rounds=1,
    )
    assert await _wait(started.run_id, {"awaiting_phase_handoff"}) == "awaiting_phase_handoff"
    handoff_id = ((orcho_run_status(started.run_id).meta or {}).get("phase_handoff") or {}).get("id")
    assert handoff_id

    first = await orcho_phase_handoff_decide(
        started.run_id, handoff_id=handoff_id, action="retry_feedback",
        feedback="Исправьте gate:pytest-unit:1 и повторите проверку.",
    )
    decision_paths = list(Path(started.run_dir).glob("phase_handoff_decisions/*.json"))
    assert len(decision_paths) == 1
    persisted_decision = decision_paths[0].read_bytes()
    replay = await orcho_phase_handoff_decide(
        started.run_id, handoff_id=handoff_id, action="retry_feedback",
        feedback="Исправьте gate:pytest-unit:1 и повторите проверку.",
    )
    assert replay.decided_at == first.decided_at
    assert replay.feedback == first.feedback
    assert "gate:pytest-unit:1" in first.feedback
    assert decision_paths[0].read_bytes() == persisted_decision
    assert _CORE_CHECKOUT is not None, "the L4 trace must use a core checkout"

    from orcho_mcp.services.run_projection import project_pending_handoff
    assert project_pending_handoff(started.run_id).decision_state == "recorded"

    # Every read surface sees a recorded decision and offers no second decide.
    reads = [
        orcho_run_status(started.run_id).model_dump_json(),
        orcho_run_events_summary(started.run_id).model_dump_json(),
        orcho_run_live_status(started.run_id).model_dump_json(),
        orcho_run_diagnose(started.run_id).model_dump_json(),
        orcho_workspace_pending_decisions().model_dump_json(),
    ]
    for index, payload in enumerate(reads):
        assert "orcho_phase_handoff_decide" not in payload, index
    # A reconnecting watcher reads the recorded decision too: it must not
    # recreate an operator decision prompt after the original transport ended.
    watch = await orcho_run_watch(started.run_id, timeout_s=1)
    assert "orcho_phase_handoff_decide" not in watch.model_dump_json()

    monkeypatch.delenv("ORCHO_MOCK_IMPLEMENT_INCOMPLETE")
    resumed = await orcho_run_resume(started.run_id)
    assert resumed.run_id == started.run_id
    assert "--resume" in resumed.command
    assert await _wait(started.run_id, {"done", "failed"}) == "done"
    # This is a core retry, not a local MCP loop: exact replay left the
    # original durable decision artifact untouched before the fresh run.
    assert decision_paths[0].read_bytes() == persisted_decision

    # No transport/watch is retained here; durable readback must still settle.
    run_dir = Path(started.run_dir)
    mcp = json.loads((run_dir / "mcp_supervisor.json").read_text(encoding="utf-8"))
    core = json.loads((run_dir / "run_supervisor.json").read_text(encoding="utf-8"))
    assert (mcp["run_id"], mcp["pid"], mcp["status"]) == (
        core["run_id"], core["pid"], core["status"],
    )
    assert mcp["status"] == "done"
    assert orcho_run_status(started.run_id).meta["status"] == "done"
    assert orcho_run_live_status(started.run_id).status == "done"
    assert orcho_run_diagnose(started.run_id).status == "done"
    assert "orcho_phase_handoff_decide" not in orcho_workspace_pending_decisions().model_dump_json()
    errors = orcho_run_evidence(started.run_id, slice="errors")
    assert errors.errors is not None
    assert errors.errors.halt_reason is None


@pytest.mark.asyncio
async def test_finalized_resume_is_preflight_refusal_without_spawn(mock_project: Path) -> None:
    """A finalized parent is refused before the supervisor can create a child."""
    from orcho_mcp.supervisor import get_supervisor
    from orcho_mcp.tools import orcho_run_resume

    run_id = "coordinated-finalized-parent"
    write_run(
        mock_project.parent,
        run_id,
        meta={"status": "done", "project": str(mock_project), "task": "settled"},
        supervisor_state=supervisor_state(
            run_id=run_id, status="done", project_dir=str(mock_project),
        ),
    )
    supervisor = get_supervisor()
    before = dict(supervisor._runs)

    result = await orcho_run_resume(run_id)

    assert result.resume_outcome == "rejected_terminal"
    assert supervisor._runs == before


class _ExitedProcess:
    """Small process double used only to prove core follow-up argv lineage."""

    pid = 424242

    def wait(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_followup_creates_distinct_child_with_core_lineage(
    mock_project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit followup reaches core and cannot reuse the parent run identity."""
    from sdk.run_control import launch as core_launch

    from orcho_mcp.tools import orcho_run_resume

    retained = mock_project.parent / "retained-worktree"
    init_git_repo(retained)
    (retained / "repair.py").write_text("fixed = True\n", encoding="utf-8")
    parent_id = "coordinated-rejected-parent"
    parent_dir = write_run(
        mock_project.parent,
        parent_id,
        meta={
            "status": "halted", "halt_reason": "final_acceptance_rejected",
            "project": str(mock_project), "task": "repair rejected change",
            "profile": "feature",
            "worktree": {
                "isolation": "worktree", "path": str(retained),
                "followup_continuity": {
                    "mode_label": "reused_parent", "blocked": False,
                    "reason": None, "diff_source": "worktree",
                },
            },
            "phases": {"final_acceptance": {"verdict": "REJECTED", "release_blockers": [{"id": "R1"}]}},
        },
    )
    spawned: list[list[str]] = []

    def fake_spawn(cmd, *, project_dir, env, log_fd):
        spawned.append(cmd)
        child_dir = parent_dir.parent / env["ORCHO_RUN_ID"]
        child_dir.mkdir(exist_ok=True)
        child_dir.joinpath("meta.json").write_text(json.dumps({
            "status": "done", "resume_mode": "followup", "parent_run_id": parent_id,
            "project": str(mock_project), "task": "repair rejected change",
        }), encoding="utf-8")
        return _ExitedProcess()

    monkeypatch.setattr(core_launch, "_spawn_detached", fake_spawn)
    result = await orcho_run_resume(
        parent_id, operator_intent="followup", operator_comment="repair the rejection",
    )

    assert result.resume_outcome == "followup_started"
    assert result.run_id != parent_id
    child = json.loads(Path(result.run_dir).joinpath("meta.json").read_text(encoding="utf-8"))
    assert child["parent_run_id"] == parent_id
    assert spawned[0][spawned[0].index("--resume") + 1] == parent_id
    assert spawned[0][spawned[0].index("--run-id") + 1] == result.run_id
    assert result.run_id != parent_id


@pytest.mark.asyncio
async def test_rc_one_settlement_survives_disconnected_readback(mock_project: Path) -> None:
    """A reaped rc=1 is consistently failed after the observer has gone away."""
    from orcho_mcp.supervisor import RunHandle, RunsSupervisor
    from orcho_mcp.supervisor.state import write_state
    from orcho_mcp.tools import (
        orcho_run_diagnose,
        orcho_run_evidence,
        orcho_run_live_status,
        orcho_run_status,
        orcho_workspace_pending_decisions,
    )

    run_id = "coordinated-rc-one"
    run_dir = write_run(
        mock_project.parent, run_id,
        meta={"status": "running", "project": str(mock_project), "task": "crash"},
    )
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(1)"], cwd=run_dir)
    handle = RunHandle(run_id, proc.pid, proc.pid, run_dir, str(mock_project), ["mock"], "t", popen=proc)
    write_state(handle)
    await RunsSupervisor()._reap(handle)

    mcp = json.loads((run_dir / "mcp_supervisor.json").read_text(encoding="utf-8"))
    core = json.loads((run_dir / "run_supervisor.json").read_text(encoding="utf-8"))
    assert (mcp["pid"], mcp["status"], mcp["exit_code"]) == (core["pid"], core["status"], 1)
    assert (orcho_run_status(run_id).meta or {})["status"] == "failed"
    assert orcho_run_live_status(run_id).status == "failed"
    assert orcho_run_diagnose(run_id).status == "failed"
    assert orcho_run_evidence(run_id, slice="errors").errors.halt_reason == "abnormal_exit:1"
    assert run_id not in orcho_workspace_pending_decisions().model_dump_json()
