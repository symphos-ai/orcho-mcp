"""Resume on a pending phase handoff returns a structured response (T1).

Before this slice, calling ``orcho_run_resume`` on a run still paused on
``awaiting_phase_handoff`` (no decision artifact yet) surfaced a raw SDK
error. The resume path now intercepts that case and returns a structured
:class:`ResumePendingDecisionResult` pointing the operator at
``orcho_phase_handoff_decide`` — never spawning the supervisor. Once a
decision artifact exists the interception is skipped and the normal
supervisor resume runs.

The supervisor is monkeypatched so a wrongful spawn is observable: the
pending-decision path must leave ``resume_kwargs`` untouched.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sdk import phase_handoff_decide

from orcho_mcp.run_control.lifecycle import resume_run
from orcho_mcp.schemas import ResumePendingDecisionResult, RunResumeResult
from orcho_mcp.supervisor import RunHandle
from orcho_mcp.tools import orcho_run_resume
from tests.fixtures.mcp_workspace import meta, supervisor_state, write_run

# T3 control guard: resume_run refuses runs MCP did not start (no durable
# mcp_supervisor.json) by raising InspectOnlyControlError. These tests exercise
# the mcp_controllable pending-decision paths, so each inspected run carries
# durable supervisor state with a resolvable project_dir.


def _controllable_state(run_id: str):
    return supervisor_state(run_id=run_id, project_dir="/p/x")


class _FakeSupervisor:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.resume_kwargs: dict | None = None

    async def resume(self, run_id: str, *, profile: str | None = None):
        self.resume_kwargs = {"run_id": run_id, "profile": profile}
        return RunHandle(
            run_id=run_id,
            pid=124,
            pgid=124,
            run_dir=self.tmp_path,
            project_dir="/p",
            command=["python", "-m", "pipeline.project_orchestrator"],
            started_at="2026-05-07T12:00:00.000Z",
        )


def _patch_supervisor(monkeypatch, fake) -> None:
    monkeypatch.setattr("orcho_mcp.supervisor.get_supervisor", lambda: fake)


def _paused_meta():
    return meta(
        status="awaiting_phase_handoff", project="/p/x", task="t",
        phase_handoff={
            "id": "validate_plan:plan_round:1",
            "phase": "validate_plan",
            "trigger": "rejected",
            "verdict": "REJECTED",
            "round": 1,
            "loop_max_rounds": 1,
            "available_actions": [
                "continue", "retry_feedback", "halt", "continue_with_waiver",
            ],
            "last_output": "Plan is missing acceptance criteria.",
        },
    )


@pytest.mark.asyncio
async def test_resume_paused_without_decision_returns_pending_decision(
    fake_workspace, tmp_path, monkeypatch,
):
    write_run(
        fake_workspace, "20260101_000001", meta=_paused_meta(),
        supervisor_state=_controllable_state("20260101_000001"),
    )
    fake = _FakeSupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    result = await resume_run("20260101_000001")

    assert isinstance(result, ResumePendingDecisionResult)
    assert result.kind == "pending_phase_handoff_decision"
    assert result.resume_outcome == "pending_decision"
    assert result.run_id == "20260101_000001"
    assert result.handoff_id == "validate_plan:plan_round:1"
    assert result.phase == "validate_plan"
    assert result.status == "awaiting_phase_handoff"
    assert result.decision_artifact_exists is False
    assert "continue" in result.available_actions
    assert "orcho_phase_handoff_decide" in result.suggested_next_action
    # A single non-optional decide follow-up, pre-filled with the ids only and
    # typed as operator-input-required (action/feedback never substituted).
    assert len(result.next_actions) == 1
    na = result.next_actions[0]
    assert na.tool == "orcho_phase_handoff_decide"
    assert na.optional is False
    assert na.kind == "operator_input_required"
    assert na.requires_operator_input is True
    assert na.choices == [
        "continue", "retry_feedback", "halt", "continue_with_waiver",
    ]
    assert na.args == {
        "run_id": "20260101_000001",
        "handoff_id": "validate_plan:plan_round:1",
    }
    assert "action" not in na.args and "feedback" not in na.args
    # The supervisor was never asked to spawn a resume subprocess.
    assert fake.resume_kwargs is None


@pytest.mark.asyncio
async def test_resume_via_tool_returns_pending_decision(
    fake_workspace, tmp_path, monkeypatch,
):
    write_run(
        fake_workspace, "20260101_000002", meta=_paused_meta(),
        supervisor_state=_controllable_state("20260101_000002"),
    )
    fake = _FakeSupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    result = await orcho_run_resume("20260101_000002")

    assert isinstance(result, ResumePendingDecisionResult)
    assert fake.resume_kwargs is None


@pytest.mark.asyncio
async def test_resume_with_recorded_decision_proceeds_to_supervisor(
    fake_workspace, tmp_path, monkeypatch,
):
    write_run(
        fake_workspace, "20260101_000003", meta=_paused_meta(),
        supervisor_state=_controllable_state("20260101_000003"),
    )
    # Record a decision via the sanctioned SDK path; ``continue`` keeps the
    # run paused (status unchanged) but the artifact now exists, so resume
    # must fall through to the supervisor rather than re-prompting.
    phase_handoff_decide(
        "20260101_000003", "validate_plan:plan_round:1", "continue", cwd=None,
    )
    fake = _FakeSupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    result = await resume_run("20260101_000003", profile="planning")

    assert isinstance(result, RunResumeResult)
    assert result.resume_outcome == "applied"
    assert result.pid == 124
    assert fake.resume_kwargs == {
        "run_id": "20260101_000003", "profile": "planning",
    }
