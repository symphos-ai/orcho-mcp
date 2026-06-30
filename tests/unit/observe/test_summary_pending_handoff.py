"""Pending-handoff visibility in ``orcho_run_events_summary`` (T1).

A polling caller should see an active phase handoff — and its decision
surface — straight from the bounded summary, without having to fire an
``orcho_run_watch`` handoff trigger. These tests pin that the
``pending_handoff`` field is populated for a paused run (with a bounded
``last_output_preview``) and stays ``None`` for a non-paused run.
"""
from __future__ import annotations

from sdk import phase_handoff_decide

from orcho_mcp.tools import orcho_run_events_summary
from tests.fixtures.mcp_workspace import meta, write_run


def _ev(seq: int, kind: str = "phase.start", phase: str = "validate_plan"):
    return {"seq": seq, "ts": f"2026-01-01T00:00:{seq:02d}", "kind": kind,
            "phase": phase, "payload": {}}


def test_summary_surfaces_pending_handoff(fake_workspace):
    long_output = "x" * 1200  # well past the 500-char preview cap
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x", task="t",
            phase_handoff={
                "id": "validate_plan:plan_round:1",
                "phase": "validate_plan",
                "trigger": "rejected",
                "verdict": "REJECTED",
                "round": 1,
                "loop_max_rounds": 1,
                "available_actions": ["continue", "retry_feedback", "halt"],
                "last_output": long_output,
            },
        ),
        events=[_ev(1)],
    )

    r = orcho_run_events_summary("20260101_000001")

    assert r.status == "awaiting_phase_handoff"
    ph = r.pending_handoff
    assert ph is not None
    assert ph.handoff_id == "validate_plan:plan_round:1"
    assert ph.phase == "validate_plan"
    assert ph.trigger == "rejected"
    assert ph.verdict == "REJECTED"
    assert ph.round_label == "validate_plan automatic round 1/1"
    assert ph.available_actions == ["continue", "retry_feedback", "halt"]
    assert ph.decision_artifact_exists is False
    assert ph.suggested_next_action is not None
    # last_output is surfaced as a bounded preview, never the full text.
    assert ph.last_output_preview is not None
    assert len(ph.last_output_preview) == 500


def test_summary_pending_handoff_decision_recorded_points_to_resume(
    fake_workspace,
):
    """Coherence with ``orcho_run_diagnose``: once a decision artifact exists
    the summary keeps surfacing the pending handoff (status is still
    ``awaiting_phase_handoff``), but ``decision_artifact_exists`` is True and
    ``suggested_next_action`` routes the captain at ``orcho_run_resume`` rather
    than another ``orcho_phase_handoff_decide``."""
    write_run(
        fake_workspace, "20260101_000003",
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x", task="t",
            phase_handoff={
                "id": "validate_plan:plan_round:1",
                "phase": "validate_plan",
                "available_actions": ["continue", "retry_feedback", "halt"],
            },
        ),
        events=[_ev(1)],
    )
    phase_handoff_decide(
        "20260101_000003", "validate_plan:plan_round:1", "continue", cwd=None,
    )

    r = orcho_run_events_summary("20260101_000003")

    assert r.status == "awaiting_phase_handoff"
    ph = r.pending_handoff
    assert ph is not None
    assert ph.decision_artifact_exists is True
    assert ph.suggested_next_action is not None
    assert "orcho_run_resume" in ph.suggested_next_action
    assert "orcho_phase_handoff_decide" not in ph.suggested_next_action


def test_summary_no_pending_handoff_when_running(fake_workspace):
    write_run(
        fake_workspace, "20260101_000002",
        meta=meta(status="running", project="/p/x", task="t"),
        events=[_ev(1, kind="phase.start")],
    )

    r = orcho_run_events_summary("20260101_000002")

    assert r.status == "running"
    assert r.pending_handoff is None
