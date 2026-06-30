"""Retry-state visibility in ``orcho_run_events_summary`` (T2).

The summary surfaces the human-retry / repeated-reject lifecycle alongside
``pending_handoff``, so a polling client can tell a repeated reject from a
first automatic reject — with the operator feedback bounded to a preview.
"""
from __future__ import annotations

import json
from pathlib import Path

from sdk import safe_handoff_id

from orcho_mcp.tools import orcho_run_events_summary
from tests.fixtures.mcp_workspace import meta, write_run


def _ev(seq, kind="phase.start", phase="validate_plan"):
    return {"seq": seq, "ts": f"2026-01-01T00:00:{seq:02d}", "kind": kind,
            "phase": phase, "payload": {}}


def _write_decision(run_dir: Path, *, run_id, handoff_id, feedback):
    ddir = run_dir / "phase_handoff_decisions"
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / f"{safe_handoff_id(handoff_id)}.json").write_text(
        json.dumps({
            "run_id": run_id,
            "handoff_id": handoff_id,
            "phase": "validate_plan",
            "action": "retry_feedback",
            "feedback": feedback,
            "note": None,
            "decided_at": "2026-01-01T00:00:00+00:00",
        }),
        encoding="utf-8",
    )


def test_summary_surfaces_repeated_reject(fake_workspace):
    long_feedback = "y" * 1200
    run_dir = write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x", task="t",
            phase_handoff={
                "id": "validate_plan:plan_round:2",
                "phase": "validate_plan",
                "round": 2,
                "loop_max_rounds": 1,
                "verdict": "REJECTED",
                "approved": False,
                "available_actions": ["continue", "retry_feedback", "halt"],
            },
        ),
        events=[_ev(1)],
    )
    _write_decision(
        run_dir, run_id="20260101_000001",
        handoff_id="validate_plan:plan_round:1", feedback=long_feedback,
    )

    r = orcho_run_events_summary("20260101_000001")

    assert r.retry_state is not None
    assert r.retry_state.retry_context == "retry_rejected_again"
    assert r.retry_state.pending_operator_decision is True
    assert "human retry 1 rejected" in r.retry_state.retry_attempt_label
    # Feedback is surfaced as a bounded preview, never the full text.
    assert r.retry_state.operator_feedback_preview is not None
    assert len(r.retry_state.operator_feedback_preview) == 500


def test_summary_no_retry_state_for_plain_running_run(fake_workspace):
    write_run(
        fake_workspace, "20260101_000002",
        meta=meta(status="running", project="/p/x", task="t"),
        events=[_ev(1, kind="phase.start", phase="plan")],
    )

    r = orcho_run_events_summary("20260101_000002")
    assert r.retry_state is None
