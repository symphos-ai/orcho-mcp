"""Unit tests for the pending-handoff projection (T1).

``project_pending_handoff`` is the single read-model behind the
status-visible handoff fields (``observe.summary``) and the structured
resume pending-decision response (``run_control.lifecycle``). It parses
the operator-significant ``meta.phase_handoff`` fields — verdict, round
counters, last output, available actions — plus a derived coherent round
label and a ``decision_artifact_exists`` probe via the SDK reader.

These tests pin: the paused projection field mapping, the structural
human-retry round label (``round > loop_max_rounds`` never renders an
impossible ``R/M``), the non-paused short-circuit, and the
decision-artifact flip (which also swings ``suggested_next_action`` from
decide→resume).
"""
from __future__ import annotations

from sdk import phase_handoff_decide

from orcho_mcp.services.run_projection import (
    project_handoff_read_model,
    project_pending_handoff,
)
from tests.fixtures.mcp_workspace import write_run


def _paused_meta(**handoff_extra):
    handoff = {
        "id": "validate_plan:plan_round:1",
        "phase": "validate_plan",
        "trigger": "rejected",
        "verdict": "REJECTED",
        "approved": False,
        "round": 1,
        "loop_max_rounds": 1,
        "available_actions": [
            "continue", "retry_feedback", "halt", "continue_with_waiver",
        ],
        "last_output": "Plan is missing acceptance criteria.",
    }
    handoff.update(handoff_extra)
    return {
        "project": "/p/x",
        "status": "awaiting_phase_handoff",
        "task": "t",
        "phase_handoff": handoff,
    }


def test_project_pending_handoff_maps_operator_fields(fake_workspace):
    write_run(fake_workspace, "20260101_000001", meta=_paused_meta())

    p = project_pending_handoff("20260101_000001")

    assert p.is_pending_handoff is True
    assert p.status == "awaiting_phase_handoff"
    assert p.handoff_id == "validate_plan:plan_round:1"
    assert p.phase == "validate_plan"
    assert p.trigger == "rejected"
    assert p.verdict == "REJECTED"
    assert p.available_actions == [
        "continue", "retry_feedback", "halt", "continue_with_waiver",
    ]
    assert p.last_output == "Plan is missing acceptance criteria."
    assert p.round_label == "validate_plan automatic round 1/1"
    assert p.decision_artifact_exists is False
    # No decision yet → suggested path is decide-then-resume.
    assert "orcho_phase_handoff_decide" in p.suggested_next_action


def test_project_pending_handoff_human_retry_round_label(fake_workspace):
    # A human-directed retry round sits on top of the auto budget
    # (round > loop_max_rounds); the label must never be ``2/1``.
    write_run(
        fake_workspace, "20260101_000002",
        meta=_paused_meta(
            id="validate_plan:plan_round:2", round=2, loop_max_rounds=1,
        ),
    )

    p = project_pending_handoff("20260101_000002")

    assert p.round_label == "validate_plan human retry 1"


def test_project_pending_handoff_not_paused_short_circuits(fake_workspace):
    write_run(
        fake_workspace, "20260101_000003",
        meta={"project": "/p/x", "status": "running", "task": "t"},
    )

    p = project_pending_handoff("20260101_000003")

    assert p.is_pending_handoff is False
    assert p.status == "running"
    assert p.handoff_id is None
    assert p.available_actions == []
    assert p.suggested_next_action is None


def test_project_pending_handoff_decision_artifact_flips_suggestion(
    fake_workspace,
):
    write_run(fake_workspace, "20260101_000004", meta=_paused_meta())
    # Record a decision artifact through the sanctioned SDK path; the run
    # stays paused (continue does not flip status) so the projection still
    # fires, but now with decision_artifact_exists=True.
    phase_handoff_decide(
        "20260101_000004", "validate_plan:plan_round:1", "continue", cwd=None,
    )

    p = project_pending_handoff("20260101_000004")

    assert p.is_pending_handoff is True
    assert p.decision_artifact_exists is True
    # Decision recorded → only resume remains.
    assert "orcho_run_resume" in p.suggested_next_action


def test_handoff_read_model_carries_new_operator_fields(fake_workspace):
    write_run(fake_workspace, "20260101_000005", meta=_paused_meta())

    rm = project_handoff_read_model("20260101_000005")

    assert rm.verdict == "REJECTED"
    assert rm.round_n == 1
    assert rm.loop_max_rounds == 1
    assert rm.last_output == "Plan is missing acceptance criteria."
    assert rm.decision_artifact_exists is False


def test_decision_read_failure_is_degraded_not_missing(fake_workspace, monkeypatch):
    write_run(fake_workspace, "20260101_000006", meta=_paused_meta())

    def _broken(*args, **kwargs):
        raise OSError("artifact unavailable")

    monkeypatch.setattr(
        "orcho_mcp.services.run_projection._sdk_load_phase_handoff_decision",
        _broken,
    )

    pending = project_pending_handoff("20260101_000006")

    assert pending.decision_state == "degraded"
    assert pending.decision_degraded_reason == "decision_artifact_read_failed"
    assert pending.decision_artifact_exists is False
    assert "orcho_phase_handoff_decide" not in pending.suggested_next_action
