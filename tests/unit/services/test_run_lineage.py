"""Follow-up lineage projection surfaced on ``orcho_run_status`` (T3).

When a parent run has a newer, still-unfinished follow-up child, resuming
the parent would diverge from the live child — the CLI tells the operator
to "Resume active follow-up <child>". These tests pin the structured MCP
equivalent: ``RunStatus.lineage`` carries parent linkage, the active-child
status, and a ``resume_child`` recommendation, with the child enumeration
matching orcho-core's terminal-status filter (terminal children and cross
sub-pipelines are excluded; the newest active child wins).
"""
from __future__ import annotations

import json

from orcho_mcp.tools import orcho_run_status
from tests.fixtures.mcp_workspace import meta, write_run


def _followup_child_meta(parent_run_id: str, *, status: str = "running", **extra):
    return meta(
        status=status, project="/p/x", task="follow-up task",
        resume_mode="followup", parent_run_id=parent_run_id, **extra,
    )


def test_status_lineage_recommends_resume_active_child(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=_followup_child_meta(
            "20260101_000001",
            status="awaiting_phase_handoff",
            phase_handoff={
                "id": "validate_plan:plan_round:1",
                "phase": "validate_plan",
                "available_actions": ["continue", "halt"],
            },
        ),
    )

    s = orcho_run_status("20260101_000001")

    lin = s.lineage
    assert lin is not None
    assert lin.run_id == "20260101_000001"
    assert lin.has_active_child_followup is True
    assert lin.active_child_run_id == "20260101_000002"
    assert lin.active_child_status == "awaiting_phase_handoff"
    assert lin.active_child_handoff_id == "validate_plan:plan_round:1"
    assert lin.recommended_action == "resume_child"
    assert lin.recommended_run_id == "20260101_000002"
    assert "Resume active follow-up 20260101_000002" in lin.recommendation


def test_status_lineage_surfaces_parent_on_child(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=_followup_child_meta("20260101_000001", status="running"),
    )

    s = orcho_run_status("20260101_000002")

    lin = s.lineage
    assert lin is not None
    assert lin.parent_run_id == "20260101_000001"
    assert lin.parent_status == "done"
    assert lin.resume_mode == "followup"
    # The child has no follow-up of its own.
    assert lin.has_active_child_followup is False
    assert lin.recommended_action is None


def test_status_lineage_ignores_terminal_child(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=_followup_child_meta("20260101_000001", status="done"),
    )

    s = orcho_run_status("20260101_000001")

    assert s.lineage is not None
    assert s.lineage.has_active_child_followup is False
    assert s.lineage.recommended_run_id is None


def test_status_lineage_terminal_halt_reason_excluded_but_other_halt_active(
    fake_workspace,
):
    # A child halted via the phase-handoff-halt terminal reason is NOT an
    # active follow-up; a child halted for any other reason still is.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=_followup_child_meta(
            "20260101_000001", status="halted",
            halt_reason="phase_handoff_halt",
        ),
    )

    s = orcho_run_status("20260101_000001")
    assert s.lineage is not None
    assert s.lineage.has_active_child_followup is False

    # Replace with a non-terminal halt reason → now active.
    (
        fake_workspace / "runspace" / "runs" / "20260101_000002" / "meta.json"
    ).write_text(
        json.dumps(
            _followup_child_meta(
                "20260101_000001", status="halted",
                halt_reason="pre_run_dirty_halt",
            ),
        ),
        encoding="utf-8",
    )

    s2 = orcho_run_status("20260101_000001")
    assert s2.lineage is not None
    assert s2.lineage.has_active_child_followup is True
    assert s2.lineage.active_child_run_id == "20260101_000002"


def test_status_lineage_excludes_commit_delivery_parked_children(fake_workspace):
    # A child halted on a parked commit-delivery gate (``commit_delivery_pending``
    # or ``commit_delivery_scope_blocked``) is terminal-for-checkpoint in core's
    # ``is_terminal_resume_parent``; the MCP replica must mirror that and NOT
    # advertise such a child as an active follow-up (otherwise it would be
    # mis-recommended as a checkpoint-resume target core/CLI consider parked).
    reasons = ("commit_delivery_pending", "commit_delivery_scope_blocked")
    for i, halt_reason in enumerate(reasons):
        parent = f"2026010{i + 1}_000001"
        child = f"2026010{i + 1}_000002"
        write_run(
            fake_workspace, parent,
            meta=meta(status="done", project="/p/x", task="t"),
        )
        write_run(
            fake_workspace, child,
            meta=_followup_child_meta(
                parent, status="halted", halt_reason=halt_reason,
            ),
        )

        s = orcho_run_status(parent)
        assert s.lineage is not None, halt_reason
        assert s.lineage.has_active_child_followup is False, halt_reason
        assert s.lineage.recommended_run_id is None, halt_reason


def test_status_lineage_ignores_cross_alias_child(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=_followup_child_meta(
            "20260101_000001", status="running", project_alias="alpha",
        ),
    )

    s = orcho_run_status("20260101_000001")
    assert s.lineage is not None
    assert s.lineage.has_active_child_followup is False


def test_status_lineage_picks_newest_active_child(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=_followup_child_meta("20260101_000001", status="running"),
    )
    write_run(
        fake_workspace, "20260101_000003",
        meta=_followup_child_meta("20260101_000001", status="running"),
    )

    s = orcho_run_status("20260101_000001")
    assert s.lineage is not None
    assert s.lineage.active_child_run_id == "20260101_000003"


def test_status_lineage_none_fields_for_plain_run(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="running", project="/p/x", task="t"),
    )

    s = orcho_run_status("20260101_000001")
    lin = s.lineage
    assert lin is not None
    assert lin.parent_run_id is None
    assert lin.resume_mode is None
    assert lin.has_active_child_followup is False
    assert lin.recommended_action is None
