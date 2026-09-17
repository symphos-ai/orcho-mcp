"""A producer-parked delivery gate is decidable in place (orcho-core ADR 0175 addendum).

The deferred-delivery producer parks a headless run as ``halted`` /
``commit_delivery_pending`` with a ``pending`` gate whose action is still
``none``. Core now treats exactly that shape as decidable now, so MCP must
offer the gate's ``orcho_delivery_decide`` calls instead of a checkpoint
resume that would only re-park the same gate. Every other stopped gate — a
resolved-but-unapplied record (``pending`` with an action) here — keeps the
resume-first route, pinned by the existing stopped-gate tests.
"""
from __future__ import annotations

from orcho_mcp.services.run_projection import project_run_diagnosis
from orcho_mcp.tools import orcho_run_diagnose, orcho_run_live_status
from tests.fixtures.mcp_workspace import commit_delivery, meta, write_run

RUN_ID = "20260907_154437"


def _parked_run(workspace, *, action: str) -> None:
    write_run(
        workspace, RUN_ID,
        meta=meta(
            status="halted", project="/p/x", task="t",
            halt_reason="commit_delivery_pending",
            phases={"final_acceptance": {"verdict": "APPROVED", "approved": True}},
            commit_delivery=commit_delivery(
                status="pending", action=action, release_verdict="APPROVED",
            ),
        ),
    )


def test_producer_park_is_a_delivery_decision(fake_workspace):
    _parked_run(fake_workspace, action="none")

    d = project_run_diagnosis(RUN_ID)

    assert d.condition == "needs_delivery_decision"
    assert d.recommended_next_action == "delivery_decision"
    assert d.delivery_gate_kind == "delivery_decision_required"
    assert set(d.available_actions) == {"approve", "apply", "skip", "halt"}


def test_producer_park_next_actions_point_at_the_gate_not_resume(fake_workspace):
    _parked_run(fake_workspace, action="none")

    diag = orcho_run_diagnose(RUN_ID)

    assert diag.condition == "needs_delivery_decision"
    tools = [na.tool for na in diag.next_actions]
    assert "orcho_delivery_gate" in tools
    assert "orcho_run_resume" not in tools


def test_producer_park_live_card_points_at_the_gate(fake_workspace):
    _parked_run(fake_workspace, action="none")

    card = orcho_run_live_status(RUN_ID)

    assert card.state_class == "terminal_halted"
    assert card.terminal is not None
    assert card.terminal.resume_meaningful is False
    assert "orcho_delivery_decide" in card.next_action
    assert "orcho_run_resume" not in card.next_action


def test_resolved_but_unapplied_gate_still_routes_to_resume(fake_workspace):
    # ``pending`` WITH an action is not the producer's park: the process
    # stopped between resolve and apply, so the lifecycle re-parks it first.
    _parked_run(fake_workspace, action="approve")

    diag = orcho_run_diagnose(RUN_ID)

    assert diag.condition == "halted"
    assert any(na.tool == "orcho_run_resume" for na in diag.next_actions)
    assert not any(na.tool == "orcho_delivery_gate" for na in diag.next_actions)
