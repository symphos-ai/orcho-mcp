"""Human-retry / repeated-reject lifecycle projection (T2).

``project_retry_state`` derives the reject / retry lifecycle from raw
persisted inputs (``meta.phase_handoff`` round counters + verdict and the
``retry_feedback`` decision artifacts) — no ``pipeline.control`` import.
These tests pin the four states, with emphasis on the repeated-reject
(second reject) scenario: ``retry_context`` and ``pending_operator_decision``.
"""
from __future__ import annotations

import json
from pathlib import Path

from sdk import safe_handoff_id

from orcho_mcp.services.run_projection import project_retry_state
from tests.fixtures.mcp_workspace import meta, write_run


def _paused_meta(*, handoff_id, round_n, loop_max, verdict="REJECTED",
                 approved=False, phase="validate_plan"):
    return meta(
        status="awaiting_phase_handoff", project="/p/x", task="t",
        phase_handoff={
            "id": handoff_id,
            "phase": phase,
            "round": round_n,
            "loop_max_rounds": loop_max,
            "verdict": verdict,
            "approved": approved,
            "available_actions": [
                "continue", "retry_feedback", "halt", "continue_with_waiver",
            ],
        },
    )


def _write_decision(run_dir: Path, *, run_id, handoff_id, action,
                    phase="validate_plan", feedback=None, note=None,
                    decided_at="2026-01-01T00:00:00+00:00"):
    ddir = run_dir / "phase_handoff_decisions"
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / f"{safe_handoff_id(handoff_id)}.json").write_text(
        json.dumps({
            "run_id": run_id,
            "handoff_id": handoff_id,
            "phase": phase,
            "action": action,
            "feedback": feedback,
            "note": note,
            "decided_at": decided_at,
        }),
        encoding="utf-8",
    )


def test_automatic_reject(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=_paused_meta(
            handoff_id="validate_plan:plan_round:1", round_n=1, loop_max=1,
        ),
    )

    rs = project_retry_state("20260101_000001")

    assert rs is not None
    assert rs.retry_context == "automatic_reject"
    assert rs.pending_operator_decision is True
    assert rs.retry_attempt_label == "validate_plan automatic round 1/1"
    assert rs.operator_feedback is None


def test_repeated_reject_second_reject(fake_workspace):
    # The human-directed retry round (round=2 > loop_max=1) was rejected
    # again — operator must decide. The prior retry feedback is surfaced.
    run_dir = write_run(
        fake_workspace, "20260101_000002",
        meta=_paused_meta(
            handoff_id="validate_plan:plan_round:2", round_n=2, loop_max=1,
        ),
    )
    _write_decision(
        run_dir, run_id="20260101_000002",
        handoff_id="validate_plan:plan_round:1", action="retry_feedback",
        feedback="Add explicit acceptance criteria.",
    )

    rs = project_retry_state("20260101_000002")

    assert rs is not None
    assert rs.retry_context == "retry_rejected_again"
    assert rs.pending_operator_decision is True
    assert "human retry 1 rejected" in rs.retry_attempt_label
    assert "operator decision required" in rs.retry_attempt_label
    assert rs.operator_feedback == "Add explicit acceptance criteria."


def test_human_retry_in_progress(fake_workspace):
    run_dir = write_run(
        fake_workspace, "20260101_000003",
        meta=meta(status="running", project="/p/x", task="t"),
    )
    _write_decision(
        run_dir, run_id="20260101_000003",
        handoff_id="validate_plan:plan_round:1", action="retry_feedback",
        feedback="Revisit the migration step.",
    )

    rs = project_retry_state("20260101_000003")

    assert rs is not None
    assert rs.retry_context == "human_retry_in_progress"
    assert rs.pending_operator_decision is False
    assert rs.retry_attempt_label == "validate_plan human retry in progress"
    assert rs.operator_feedback == "Revisit the migration step."


def test_retry_accepted_closed(fake_workspace):
    run_dir = write_run(
        fake_workspace, "20260101_000004",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    _write_decision(
        run_dir, run_id="20260101_000004",
        handoff_id="validate_plan:plan_round:1", action="retry_feedback",
        feedback="Fix it.",
    )

    rs = project_retry_state("20260101_000004")

    assert rs is not None
    assert rs.retry_context == "retry_accepted_closed"
    assert rs.pending_operator_decision is False
    assert "handoff closed" in rs.retry_attempt_label


def test_no_retry_lifecycle_returns_none(fake_workspace):
    write_run(
        fake_workspace, "20260101_000005",
        meta=meta(status="running", project="/p/x", task="t"),
    )

    assert project_retry_state("20260101_000005") is None


def test_pending_false_when_active_handoff_already_decided(fake_workspace):
    # Paused on an automatic reject but a decision for the active handoff
    # already exists (torn / awaiting-resume) → not pending.
    run_dir = write_run(
        fake_workspace, "20260101_000006",
        meta=_paused_meta(
            handoff_id="validate_plan:plan_round:1", round_n=1, loop_max=1,
        ),
    )
    _write_decision(
        run_dir, run_id="20260101_000006",
        handoff_id="validate_plan:plan_round:1", action="continue",
    )

    rs = project_retry_state("20260101_000006")

    assert rs is not None
    assert rs.retry_context == "automatic_reject"
    assert rs.pending_operator_decision is False


def test_paused_but_approved_is_not_a_retry_lifecycle(fake_workspace):
    # An always-policy pause on an APPROVED verdict is not a reject — no
    # retry state.
    write_run(
        fake_workspace, "20260101_000007",
        meta=_paused_meta(
            handoff_id="validate_plan:plan_round:1", round_n=1, loop_max=1,
            verdict="APPROVED", approved=True,
        ),
    )

    assert project_retry_state("20260101_000007") is None
