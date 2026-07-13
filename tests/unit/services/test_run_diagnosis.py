"""Unified run-diagnosis projection (T1).

``project_run_diagnosis`` is the single typed classifier shared by the
resume pre-flight guard (GC-2) and ``orcho_run_diagnose`` (GC-1). These
tests pin all six priority branches and the deterministic priority order:
``needs_decision`` outranks everything, an active follow-up child
supersedes an otherwise-inert terminal parent, and a blocked follow-up
worktree recommends the parent only when the parent is known.
"""
from __future__ import annotations

import pytest
from sdk import phase_handoff_decide
from sdk.run_control import RecoveryLineage, RunDiagnosis

from orcho_mcp.errors import RunNotFoundError
from orcho_mcp.services import run_projection
from orcho_mcp.services.run_projection import project_run_diagnosis
from orcho_mcp.tools import orcho_run_diagnose
from tests.fixtures.mcp_workspace import meta, supervisor_state, write_run

_RETAINED_WORKTREE = {"isolation": "worktree", "path": "/tmp/wt/source"}
_PARSED_PLAN = {"tasks": [{"id": "T1", "spec": "do the thing"}]}

_BLOCKED_WORKTREE = {
    "isolation": "worktree",
    "path": "/tmp/wt/child",
    "followup_continuity": {
        "blocked": True,
        "diff_source": "artifact",
        "reason": (
            "parent's undelivered diff exists only as a diff.patch artifact; "
            "resuming the parent recovers it"
        ),
        "mode_label": "blocked_parent_diff_unavailable",
    },
}


def _followup_child(parent_run_id: str, *, status: str = "running", **extra):
    return meta(
        status=status, project="/p/x", task="follow-up",
        resume_mode="followup", parent_run_id=parent_run_id, **extra,
    )


def test_needs_decision_branch(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x", task="t",
            phase_handoff={
                "id": "validate_plan:plan_round:1",
                "phase": "validate_plan",
                "available_actions": ["continue", "halt"],
            },
        ),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "needs_decision"
    assert d.handoff_id == "validate_plan:plan_round:1"
    assert d.available_actions == ["continue", "halt"]
    assert d.reason
    assert d.recommended_action is None
    # No decision recorded yet — the projection flag stays False.
    assert d.decision_artifact_exists is False


def test_needs_decision_with_recorded_decision_flags_resume(fake_workspace):
    """Once a decision artifact exists, the projection still classifies the
    paused run as ``needs_decision`` (status is unchanged) but sets
    ``decision_artifact_exists`` and refines the reason toward resume."""
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x", task="t",
            phase_handoff={
                "id": "validate_plan:plan_round:1",
                "phase": "validate_plan",
                "available_actions": ["continue", "retry_feedback", "halt"],
            },
        ),
    )
    # ``continue`` records the decision artifact without flipping status —
    # the run stays awaiting_phase_handoff (existing decide semantics).
    phase_handoff_decide(
        "20260101_000001", "validate_plan:plan_round:1", "continue", cwd=None,
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "needs_decision"
    assert d.status == "awaiting_phase_handoff"
    assert d.decision_artifact_exists is True
    # Reason now points the captain at resume, not another decide.
    assert "resume to continue" in d.reason


def test_superseded_by_child_branch(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=_followup_child("20260101_000001", status="running"),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "superseded_by_child"
    assert d.recommended_run_id == "20260101_000002"
    assert d.recommended_action == "resume_child"


def test_superseded_outranks_resume_inert_terminal(fake_workspace):
    # Parent is terminal-success (resume-inert) but a live follow-up child
    # exists: superseded_by_child must win over resume_inert_terminal.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="completed", project="/p/x", task="t"),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=_followup_child("20260101_000001", status="running"),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "superseded_by_child"


def test_needs_decision_outranks_superseded(fake_workspace):
    # A paused run that also has a live follow-up child must report the
    # decision first — needs_decision is the top priority.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x", task="t",
            phase_handoff={
                "id": "implement:round:2",
                "available_actions": ["continue", "halt"],
            },
        ),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=_followup_child("20260101_000001", status="running"),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "needs_decision"
    assert d.handoff_id == "implement:round:2"


def test_blocked_worktree_with_parent_recommends_parent(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=_followup_child(
            "20260101_000001", status="failed", worktree=_BLOCKED_WORKTREE,
        ),
    )

    d = project_run_diagnosis("20260101_000002")

    assert d.condition == "blocked_worktree"
    assert d.blocked is True
    assert d.parent_run_id == "20260101_000001"
    assert d.recommended_run_id == "20260101_000001"
    assert d.block_message and "diff.patch" in d.block_message


def test_blocked_worktree_without_parent_recommends_none(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="failed", project="/p/x", task="t",
            worktree=_BLOCKED_WORKTREE,
        ),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "blocked_worktree"
    assert d.parent_run_id is None
    assert d.recommended_run_id is None


def test_resume_inert_terminal_branch(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "resume_inert_terminal"
    assert d.recommended_run_id is None


def test_resume_inert_terminal_halt_reason(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="halted", project="/p/x", task="t",
            halt_reason="phase_handoff_halt",
        ),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "resume_inert_terminal"
    assert d.halt_reason == "phase_handoff_halt"


def test_resume_inert_terminal_from_supervisor_stale_meta(fake_workspace):
    # The supervisor reaped the run terminal ('done') but ``meta.json`` is
    # stale on 'running'. The terminal guard must read the MERGED status, not
    # the raw meta, so this resolves to resume_inert_terminal — not the
    # residual 'running'/'done' branch that would let a no-op resume spawn.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="running", project="/p/x", task="t"),
        supervisor_state=supervisor_state(
            run_id="20260101_000001", status="done",
        ),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "resume_inert_terminal"
    assert d.status == "done"


def test_resume_inert_terminal_from_supervisor_stale_meta_halted(fake_workspace):
    # Same stale-meta hazard via a supervisor-recorded terminal halt: merged
    # status='halted' + merged halt_reason='phase_handoff_halt' must classify
    # as resume_inert_terminal even though meta.status is still 'running'.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="running", project="/p/x", task="t"),
        supervisor_state=supervisor_state(
            run_id="20260101_000001", status="halted",
            halt_reason="phase_handoff_halt",
        ),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "resume_inert_terminal"
    assert d.status == "halted"
    assert d.halt_reason == "phase_handoff_halt"


def test_active_branch(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="running", project="/p/x", task="t"),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "active"


@pytest.mark.parametrize(
    "status, halt_reason",
    [
        ("halted", "pre_run_dirty_halt"),
        ("failed", None),
        ("interrupted", None),
    ],
)
def test_residual_resumable_branch(fake_workspace, status, halt_reason):
    extra = {"halt_reason": halt_reason} if halt_reason else {}
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status=status, project="/p/x", task="t", **extra),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == status
    assert d.status == status
    assert "resumable" in d.reason


def test_missing_run_propagates(fake_workspace):
    from orcho_mcp.services.run_lookup import RunNotFoundError

    with pytest.raises(RunNotFoundError):
        project_run_diagnosis("20260101_999999")


# ── orcho_run_diagnose tool (GC-1 / T3) ─────────────────────────────────────
#
# The tool packs project_run_diagnosis into a typed RunDiagnosis. These tests
# assert the typed ``kind`` / ``requires_operator_input`` fields of each
# next_action (never the human-readable ``intent``), the ready_call required
# args, and that no public-tool arg ever uses ``parent_run_id`` as a key.


def _decide_records(diag):
    return [na for na in diag.next_actions if na.tool == "orcho_phase_handoff_decide"]


def _assert_no_parent_run_id_arg(diag):
    for na in diag.next_actions:
        assert "parent_run_id" not in na.args, (
            f"next_action for {na.tool} leaked a parent_run_id arg: {na.args}"
        )


@pytest.mark.asyncio
async def test_diagnose_tool_is_registered_l2():
    """L2: the tool is registered and visible via in-process list_tools."""
    from orcho_mcp.instance import mcp

    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert "orcho_run_diagnose" in names


def test_diagnose_missing_run_raises(fake_workspace):
    with pytest.raises(RunNotFoundError):
        orcho_run_diagnose("20260101_999999")


def test_diagnose_needs_decision_typed_actions(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x", task="t",
            phase_handoff={
                "id": "validate_plan:plan_round:1",
                "phase": "validate_plan",
                "available_actions": [
                    "continue", "retry_feedback", "halt", "continue_with_waiver",
                ],
            },
        ),
    )

    diag = orcho_run_diagnose("20260101_000001")

    assert diag.condition == "needs_decision"
    assert diag.available_actions == [
        "continue", "retry_feedback", "halt", "continue_with_waiver",
    ]

    decides = _decide_records(diag)
    # No decide record is a ready_call without a substituted action.
    for na in decides:
        if na.kind == "ready_call":
            assert na.args.get("action") in ("continue", "halt")
            # ready_call carries every required arg of the decide signature.
            assert set(na.args) >= {"run_id", "handoff_id", "action"}

    # continue / halt → ready_call.
    ready_actions = {
        na.args["action"] for na in decides if na.kind == "ready_call"
    }
    assert ready_actions == {"continue", "halt"}

    # retry_feedback / continue_with_waiver → operator_input_required, carrying
    # choices and the feedback input_schema, never asserted ready_call.
    oir = [na for na in decides if na.kind == "operator_input_required"]
    assert {na.args["action"] for na in oir} == {
        "retry_feedback", "continue_with_waiver",
    }
    for na in oir:
        assert na.requires_operator_input is True
        assert na.choices  # non-empty
        assert na.input_schema and "feedback" in na.input_schema["properties"]
    # Verified via the typed kind field, not intent text.
    assert all(na.tool == "orcho_phase_handoff_decide" for na in decides)
    # No decision recorded yet — the wire flag stays False and no resume
    # record is offered.
    assert diag.decision_recorded is False
    assert all(na.tool != "orcho_run_resume" for na in diag.next_actions)


def test_diagnose_needs_decision_recorded_routes_to_resume(fake_workspace):
    """After a decision artifact is recorded, ``orcho_run_diagnose`` routes the
    captain to a ready ``orcho_run_resume`` and drops the decide verbs — the
    run still needs action, but resume (apply the recorded decision), not a
    second decide."""
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x", task="t",
            phase_handoff={
                "id": "validate_plan:plan_round:1",
                "phase": "validate_plan",
                "available_actions": [
                    "continue", "retry_feedback", "halt", "continue_with_waiver",
                ],
            },
        ),
    )
    phase_handoff_decide(
        "20260101_000001", "validate_plan:plan_round:1", "continue", cwd=None,
    )

    diag = orcho_run_diagnose("20260101_000001")

    assert diag.condition == "needs_decision"
    assert diag.decision_recorded is True
    # A ready resume is present and carries the full required arg set.
    resumes = [na for na in diag.next_actions if na.tool == "orcho_run_resume"]
    assert len(resumes) == 1
    resume = resumes[0]
    assert resume.kind == "ready_call"
    assert resume.requires_operator_input is False
    assert resume.optional is False
    assert resume.args == {"run_id": "20260101_000001"}
    # No decide records at all — neither ready_call nor operator_input_required.
    assert _decide_records(diag) == []
    _assert_no_parent_run_id_arg(diag)


def test_diagnose_superseded_by_child_ready_call(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=_followup_child("20260101_000001", status="running"),
    )

    diag = orcho_run_diagnose("20260101_000001")

    assert diag.condition == "superseded_by_child"
    assert diag.recommended_run_id == "20260101_000002"
    assert len(diag.next_actions) == 1
    na = diag.next_actions[0]
    assert na.kind == "ready_call"
    assert na.requires_operator_input is False
    assert na.tool == "orcho_run_resume"
    assert na.args == {"run_id": "20260101_000002"}


def test_diagnose_resume_inert_terminal_inspection_only(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
    )

    diag = orcho_run_diagnose("20260101_000001")

    assert diag.condition == "resume_inert_terminal"
    tools = {na.tool for na in diag.next_actions}
    assert tools == {"orcho_run_evidence", "orcho_run_status"}
    # Never a resume for a terminal run.
    assert all(na.tool != "orcho_run_resume" for na in diag.next_actions)
    assert all(na.kind == "ready_call" for na in diag.next_actions)


def test_diagnose_blocked_worktree_with_parent_resumes_parent(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=_followup_child(
            "20260101_000001", status="failed", worktree=_BLOCKED_WORKTREE,
        ),
    )

    diag = orcho_run_diagnose("20260101_000002")

    assert diag.condition == "blocked_worktree"
    assert diag.recommended_run_id == "20260101_000001"
    assert len(diag.next_actions) == 1
    na = diag.next_actions[0]
    assert na.kind == "ready_call"
    assert na.tool == "orcho_run_resume"
    # Parent rides as the run_id arg — never a bespoke parent_run_id parameter.
    assert na.args == {"run_id": "20260101_000001"}
    _assert_no_parent_run_id_arg(diag)


def test_diagnose_blocked_worktree_without_parent_readonly_fallback(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="failed", project="/p/x", task="t",
            worktree=_BLOCKED_WORKTREE,
        ),
    )

    diag = orcho_run_diagnose("20260101_000001")

    assert diag.condition == "blocked_worktree"
    assert diag.recommended_run_id is None
    tools = {na.tool for na in diag.next_actions}
    assert tools == {"orcho_run_status", "orcho_run_evidence"}
    # No resume offered when the parent is unknown.
    assert all(na.tool != "orcho_run_resume" for na in diag.next_actions)
    assert all(na.kind == "ready_call" for na in diag.next_actions)
    _assert_no_parent_run_id_arg(diag)


def test_diagnose_active_watch_and_status(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="running", project="/p/x", task="t"),
    )

    diag = orcho_run_diagnose("20260101_000001")

    assert diag.condition == "active"
    tools = {na.tool for na in diag.next_actions}
    assert tools == {"orcho_run_watch", "orcho_run_status"}
    assert all(na.kind == "ready_call" for na in diag.next_actions)
    assert all(na.args == {"run_id": "20260101_000001"} for na in diag.next_actions)


@pytest.mark.parametrize(
    "status, halt_reason",
    [
        ("halted", "pre_run_dirty_halt"),
        ("failed", None),
        ("interrupted", None),
    ],
)
def test_diagnose_resumable_offers_resume_and_evidence(
    fake_workspace, status, halt_reason,
):
    extra = {"halt_reason": halt_reason} if halt_reason else {}
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status=status, project="/p/x", task="t", **extra),
    )

    diag = orcho_run_diagnose("20260101_000001")

    assert diag.condition == status
    tools = {na.tool for na in diag.next_actions}
    assert tools == {"orcho_run_resume", "orcho_run_evidence"}
    resume = next(na for na in diag.next_actions if na.tool == "orcho_run_resume")
    assert resume.kind == "ready_call"
    assert resume.args == {"run_id": "20260101_000001"}


# ── recover_via_source_run branch (T2) ──────────────────────────────────────
#
# A terminal / rejected recovery run whose durable lineage points at a
# resumable source must classify as recover_via_source_run and recommend the
# SOURCE, never a fresh from_run_plan against the inert run (the dogfood
# incident: terminal recovery 474b85 should have resumed source fc2da4).


def _source_failed_with_worktree(**extra):
    return meta(
        status="failed", project="/p/x", task="source task",
        worktree=_RETAINED_WORKTREE, **extra,
    )


def test_recover_via_source_run_dogfood_shape(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=_source_failed_with_worktree(),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=meta(
            status="halted", project="/p/x", task="recovery",
            halt_reason="phase_handoff_halt",
            resume_mode="followup", parent_run_id="20260101_000001",
        ),
    )

    d = project_run_diagnosis("20260101_000002")

    assert d.condition == "recover_via_source_run"
    assert d.continuation_subject == "source_run_checkpoint"
    assert d.recommended_next_action == "resume_source_run"
    assert d.recommended_run_id == "20260101_000001"
    assert d.source_run_id == "20260101_000001"
    assert d.recovery_lineage is not None
    assert d.recovery_lineage.source_resumable is True


def test_recover_via_source_run_priority_after_delivery_gate(fake_workspace):
    # A pending delivery gate still outranks recover_via_source_run: the gate
    # is a live decision, not a dead-end.
    write_run(
        fake_workspace, "20260101_000001",
        meta=_source_failed_with_worktree(),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=meta(
            status="halted", project="/p/x", task="recovery",
            halt_reason="commit_delivery_pending",
            resume_mode="followup", parent_run_id="20260101_000001",
            commit_delivery=_commit_delivery(
                status="pending", release_verdict="APPROVED", action="approve",
            ),
        ),
    )

    d = project_run_diagnosis("20260101_000002")

    assert d.condition == "needs_delivery_decision"


def test_recover_via_source_run_tool_recommends_source_resume(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=_source_failed_with_worktree(),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=meta(
            status="halted", project="/p/x", task="recovery",
            halt_reason="phase_handoff_halt",
            resume_mode="followup", parent_run_id="20260101_000001",
        ),
    )

    diag = orcho_run_diagnose("20260101_000002")

    assert diag.condition == "recover_via_source_run"
    assert diag.continuation_subject == "source_run_checkpoint"
    assert diag.recommended_next_action == "resume_source_run"
    resumes = [na for na in diag.next_actions if na.tool == "orcho_run_resume"]
    assert len(resumes) == 1
    assert resumes[0].kind == "ready_call"
    assert resumes[0].args == {"run_id": "20260101_000001"}
    # Never offer from_run_plan / orcho_run_start to finish the inert run.
    assert all(na.tool != "orcho_run_start" for na in diag.next_actions)
    for na in diag.next_actions:
        assert "from_run_plan" not in na.args
    _assert_no_parent_run_id_arg(diag)


def test_ordinary_failed_child_with_resumable_parent_stays_resumable(fake_workspace):
    # Regression: an ordinary resumable failed child with a resumable parent is
    # NOT a terminal dead-end — it keeps recommending resume of the child
    # itself, never switching to recover_via_source_run.
    write_run(
        fake_workspace, "20260101_000001",
        meta=_source_failed_with_worktree(),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=meta(
            status="failed", project="/p/x", task="child",
            resume_mode="followup", parent_run_id="20260101_000001",
        ),
    )

    d = project_run_diagnosis("20260101_000002")

    assert d.condition == "failed"
    assert d.continuation_subject is None

    diag = orcho_run_diagnose("20260101_000002")
    assert diag.condition == "failed"
    resume = next(na for na in diag.next_actions if na.tool == "orcho_run_resume")
    assert resume.args == {"run_id": "20260101_000002"}


# ── resume_inert_terminal lineage enrichment: plan-only / stop_unknown (T2) ──


def test_plan_only_terminal_recommends_plan_artifact_continuation(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="done", project="/p/x", task="plan it",
            profile="planning", plan_source="local",
        ),
        parsed_plan=_PARSED_PLAN,
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "resume_inert_terminal"
    assert d.continuation_subject == "plan_artifact"
    assert d.recommended_next_action == "plan_artifact_continuation"

    diag = orcho_run_diagnose("20260101_000001")
    assert diag.recommended_next_action == "plan_artifact_continuation"
    starts = [na for na in diag.next_actions if na.tool == "orcho_run_start"]
    assert len(starts) == 1
    assert starts[0].kind == "ready_call"
    assert starts[0].args == {
        "from_run_plan": "20260101_000001", "profile": "feature",
    }
    # The intent must mark this as a fresh implementation from the plan.
    assert "plan" in starts[0].intent.lower()


def test_terminal_deadend_recommends_stop_unknown_no_from_run_plan(fake_workspace):
    # Case D: terminal halted run with no source / child / gate / plan must
    # stop with the missing facts and NEVER offer from_run_plan as a fallback.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="halted", project="/p/x", task="t",
            halt_reason="phase_handoff_halt", profile="feature",
        ),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "resume_inert_terminal"
    assert d.continuation_subject == "unknown"
    assert d.recommended_next_action == "stop_unknown"
    assert d.missing_facts

    diag = orcho_run_diagnose("20260101_000001")
    assert diag.continuation_subject == "unknown"
    assert diag.recommended_next_action == "stop_unknown"
    # No from_run_plan / orcho_run_start — only read-only inspection.
    assert all(na.tool != "orcho_run_start" for na in diag.next_actions)
    tools = {na.tool for na in diag.next_actions}
    assert tools <= {"orcho_run_status", "orcho_run_evidence"}
    assert diag.recovery_lineage is not None
    assert diag.recovery_lineage.missing_facts


def test_rejected_non_decidable_deadend_stops_unknown_not_resumable(fake_workspace):
    # Regression (review F1): a closed rejected delivery state — halted with a
    # NON-terminal halt_reason (so _is_terminal_resume_parent is False) but a
    # rejected release verdict and no decidable gate / source / child / plan —
    # must classify as resume_inert_terminal + stop_unknown, NOT fall into the
    # residual 'halted' branch and offer a resume of the inert run. Keeps the
    # diagnose surface consistent with the lineage / status projection.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="halted", project="/p/x", task="recovery",
            halt_reason="commit_delivery_failed",
            commit_delivery=_commit_delivery(
                status="rejected", release_verdict="REJECTED", action="skip",
            ),
        ),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "resume_inert_terminal"
    assert d.continuation_subject == "unknown"
    assert d.recommended_next_action == "stop_unknown"
    assert d.missing_facts

    diag = orcho_run_diagnose("20260101_000001")
    assert diag.condition == "resume_inert_terminal"
    assert diag.recommended_next_action == "stop_unknown"
    # Never a resume of the inert run, never a from_run_plan fallback.
    assert all(na.tool != "orcho_run_resume" for na in diag.next_actions)
    assert all(na.tool != "orcho_run_start" for na in diag.next_actions)
    tools = {na.tool for na in diag.next_actions}
    assert tools <= {"orcho_run_status", "orcho_run_evidence"}


def test_clean_terminal_success_unchanged_inspection_only(fake_workspace):
    # Guard: a clean terminal-success run keeps inspection-only next_actions
    # (start_followup subject, no resume of the terminal run).
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t", profile="feature"),
    )

    diag = orcho_run_diagnose("20260101_000001")

    assert diag.condition == "resume_inert_terminal"
    assert diag.recommended_next_action == "start_followup"
    assert all(na.tool != "orcho_run_resume" for na in diag.next_actions)
    assert all(na.tool != "orcho_run_start" for na in diag.next_actions)


# ── delivery / correction gate enrichment (T3) ──────────────────────────────


def _commit_delivery(*, status, release_verdict, action):
    return {
        "status": status,
        "action": action,
        "release_verdict": release_verdict,
        "project_path": "/p/x",
        "source_path": "/p/wt",
        "changed_paths": ["src/a.py"],
        "untracked_paths": [],
    }


def test_projection_flags_correction_followup_over_terminal_halt(fake_workspace):
    # Correction-followup contract: a ``commit_decision_fix`` halt is a terminal *halt_reason* but
    # really a correction whose fix was already requested. The gate branch must
    # win over resume_inert_terminal AND classify as correction_followup_required
    # (the next step is a from_run_plan follow-up, not another delivery decide).
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="halted", project="/p/x", task="t",
            halt_reason="commit_decision_fix",
            commit_delivery=_commit_delivery(
                status="fix_requested", release_verdict="REJECTED", action="fix",
            ),
        ),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "correction_followup_required"
    assert d.delivery_gate_kind == "correction_decision_required"
    assert d.available_actions == ["halt"]
    assert d.recommended_next_action == "start_followup"
    assert d.continuation_subject == "plan_artifact"
    assert d.followup_project_dir == "/p/x"


def test_diagnose_delivery_gate_points_to_gate_projection(fake_workspace):
    # A real (approved) pending delivery gate still points at orcho_delivery_gate;
    # the fix-correction follow-up case is covered separately.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="halted", project="/p/x", task="t",
            halt_reason="commit_delivery_pending",
            commit_delivery=_commit_delivery(
                status="pending", release_verdict="APPROVED", action="approve",
            ),
        ),
    )

    diag = orcho_run_diagnose("20260101_000001")

    assert diag.condition == "needs_delivery_decision"
    assert diag.next_actions, "delivery gate must surface a next action"
    for na in diag.next_actions:
        assert na.kind == "ready_call"
        assert na.requires_operator_input is False
        assert na.tool == "orcho_delivery_gate"
        assert na.args == {"run_id": "20260101_000001"}
    assert "feedback" not in str(diag.next_actions[0].args)


def test_no_commit_delivery_keeps_existing_terminal_classification(fake_workspace):
    # Guard: a plain terminal run (no commit_delivery) is unaffected by the
    # new gate branch.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
    )

    d = project_run_diagnosis("20260101_000001")
    assert d.condition == "resume_inert_terminal"
    assert d.delivery_gate_kind is None


def test_delivery_completed_gate_kind_points_to_gate_never_a_decision():
    # A ``delivery_completed`` gate is terminal — the Orcho-managed delivery
    # already landed. Diagnose stays read-only: it points at the gate projection
    # for the delivered outcome (pr_url / delivery notices) and NEVER offers an
    # orcho_delivery_decide call. Exercises the T1 terminal branch directly (the
    # wire only surfaces this kind defensively, never for a normal terminal).
    from orcho_mcp.inspection.diagnosis import _delivery_gate_actions

    actions = _delivery_gate_actions("20260101_000001", "delivery_completed", [])

    assert len(actions) == 1
    only = actions[0]
    assert only.tool == "orcho_delivery_gate"
    assert only.args == {"run_id": "20260101_000001"}
    assert only.kind == "ready_call"
    # Terminal: no delivery decision is advertised, and the intent names the
    # delivered outcome (pr_url), not a choose-a-decision prompt.
    assert all(a.tool != "orcho_delivery_decide" for a in actions)
    assert "already landed" in only.intent
    assert "pr_url" in only.intent


# ── Correction-followup contract: correction_followup_required + superseded parent ───────────────


def test_diagnose_correction_followup_emits_from_run_plan_action(fake_workspace):
    # After fix, diagnose surfaces a typed orcho_run_start from_run_plan action
    # carrying the retained diff as context (not as a tool arg), and never a
    # resume of this inert run.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="halted", project="/p/x", task="t",
            halt_reason="commit_decision_fix",
            commit_delivery=_commit_delivery(
                status="fix_requested", release_verdict="REJECTED", action="fix",
            ),
        ),
        diff_patch="diff --git a/src/a.py b/src/a.py\n",
    )

    diag = orcho_run_diagnose("20260101_000001")

    assert diag.condition == "correction_followup_required"
    assert diag.recommended_next_action == "start_followup"
    starts = [na for na in diag.next_actions if na.tool == "orcho_run_start"]
    assert len(starts) == 1
    assert starts[0].kind == "ready_call"
    assert starts[0].requires_operator_input is False
    assert starts[0].args["from_run_plan"] == "20260101_000001"
    assert starts[0].args.get("project_dir") == "/p/x"
    assert "action" not in starts[0].args
    # The retained diff path + checkout context ride as typed, machine-readable
    # ``context`` — NOT as prose in the (non-contractual) intent. A typed client
    # reads the diff/worktree pointers from these structured keys.
    ctx = starts[0].context or {}
    assert ctx.get("from_run_plan") == "20260101_000001"
    assert ctx.get("project_dir") == "/p/x"
    assert str(ctx.get("diff_path", "")).endswith("diff.patch")
    # Never a bare resume of the inert parent.
    assert all(na.tool != "orcho_run_resume" for na in diag.next_actions)


def _superseded_parent_meta():
    # Mirrors what orcho-core finalization (the cross-run supersession finalization) leaves behind: a
    # rejected-FA parent settled to done + superseded_by_followup, with the
    # phantom commit_delivery evicted and only the historical rejected phase.
    return meta(
        status="done", project="/p/x", task="t",
        superseded_by_followup={
            "child_run_id": "20260101_000002",
            "child_status": "done",
            "delivery_status": "committed",
            "reason": "correction delivered via from_run_plan follow-up",
        },
        phases={
            "final_acceptance": {
                "verdict": "REJECTED",
                "release_blockers": [{"id": "RB1", "detail": "data loss"}],
            },
        },
    )


def test_superseded_parent_is_closed_not_active_correction(fake_workspace):
    write_run(fake_workspace, "20260101_000001", meta=_superseded_parent_meta())

    d = project_run_diagnosis("20260101_000001")

    # Closed by a successful follow-up — a DISTINCT typed condition, never a
    # generic resume_inert_terminal and never an active correction candidate.
    assert d.condition == "closed_by_followup"
    assert d.delivery_gate_kind is None
    assert d.recommended_run_id == "20260101_000002"
    assert "superseded" in d.reason
    assert "20260101_000002" in d.reason


def test_diagnose_superseded_parent_inspection_only(fake_workspace):
    write_run(fake_workspace, "20260101_000001", meta=_superseded_parent_meta())

    diag = orcho_run_diagnose("20260101_000001")

    # Typed closed/superseded state — distinguishable from any other inert
    # terminal — with the superseding child carried for inspection.
    assert diag.condition == "closed_by_followup"
    assert diag.recommended_run_id == "20260101_000002"
    # No from_run_plan / resume offered for a closed superseded parent.
    assert all(na.tool != "orcho_run_start" for na in diag.next_actions)
    assert all(na.tool != "orcho_run_resume" for na in diag.next_actions)
    for na in diag.next_actions:
        assert "from_run_plan" not in na.args
    # The superseding child is the inspection subject.
    assert any(
        na.args.get("run_id") == "20260101_000002" for na in diag.next_actions
    )


# ── Migration contract — thin projection of core ``run_diagnosis`` ───────────


def test_maps_core_run_diagnosis_field_for_field(fake_workspace, monkeypatch):
    # The projection maps every published core ``RunDiagnosis`` field onto
    # ``RunDiagnosisProjection``: ``missing_facts``/``available_actions`` tuple →
    # list, the attached ``recovery`` → nested ``recovery_lineage`` via T1's
    # mapper, and the MCP-only ``parent_run_id`` overlaid. Core is stubbed so the
    # mapping is asserted in isolation; the call wiring (cwd=None +
    # supervisor-merged meta + source_meta) is captured.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="running", project="/p/x", task="t",
            parent_run_id="20260101_000000",
        ),
        supervisor_state=supervisor_state(
            run_id="20260101_000001", status="halted",
            halt_reason="phase_handoff_halt",
        ),
    )
    captured: dict = {}

    def fake_run_diagnosis(run_id, *, cwd=None, meta=None, source_meta=None,
                           workspace=None, runs_dir=None):
        captured["cwd"] = cwd
        captured["meta"] = meta
        captured["source_meta"] = source_meta
        recovery = RecoveryLineage(
            run_id=run_id,
            is_terminal_or_rejected=True,
            continuation_subject="unknown",
            recommended_next_action="stop_unknown",
            recommended_run_id=None,
            source_run_id="20260101_000000",
            source_status="failed",
            source_resumable=False,
            source_worktree_preserved=False,
            plan_subject_available=False,
            active_child_run_id=None,
            missing_facts=("no plan artifact", "no active child"),
            reason="recovery reason",
        )
        return RunDiagnosis(
            run_id=run_id,
            condition="resume_inert_terminal",
            reason="terminal reason",
            status="halted",
            halt_reason="phase_handoff_halt",
            continuation_subject="unknown",
            recommended_next_action="stop_unknown",
            recommended_run_id=None,
            source_run_id="20260101_000000",
            missing_facts=("no source/parent run id", "no plan artifact"),
            handoff_id=None,
            available_actions=(),
            delivery_gate_kind=None,
            blocked=False,
            block_message=None,
            recovery=recovery,
        )

    monkeypatch.setattr(run_projection, "_sdk_run_diagnosis", fake_run_diagnosis)

    d = project_run_diagnosis("20260101_000001")

    # Core condition fields carried verbatim.
    assert d.condition == "resume_inert_terminal"
    assert d.reason == "terminal reason"
    assert d.status == "halted"
    assert d.halt_reason == "phase_handoff_halt"
    assert d.continuation_subject == "unknown"
    assert d.recommended_next_action == "stop_unknown"
    assert d.source_run_id == "20260101_000000"
    # ``missing_facts`` core tuple → wire list (same values, same order).
    assert d.missing_facts == ["no source/parent run id", "no plan artifact"]
    assert isinstance(d.missing_facts, list)
    # MCP-only ``parent_run_id`` overlay (not a core RunDiagnosis field).
    assert d.parent_run_id == "20260101_000000"
    # Attached recovery → nested projection via the T1 mapper (tuple → list).
    assert d.recovery_lineage is not None
    assert d.recovery_lineage.continuation_subject == "unknown"
    assert d.recovery_lineage.missing_facts == [
        "no plan artifact", "no active child",
    ]
    assert isinstance(d.recovery_lineage.missing_facts, list)
    # Core is fed walk-up-disabled resolution, supervisor-merged inspected meta,
    # and a source_meta seam.
    assert captured["cwd"] is None
    assert captured["meta"]["status"] == "halted"
    assert captured["meta"]["halt_reason"] == "phase_handoff_halt"
    assert isinstance(captured["source_meta"], dict)


@pytest.mark.parametrize(
    "condition, extra_fields",
    [
        ("needs_decision", {"handoff_id": "h:1"}),
        ("superseded_by_child", {"recommended_run_id": "20260101_000002"}),
        ("blocked_worktree", {"blocked": True, "block_message": "blocked"}),
        ("needs_delivery_decision", {"delivery_gate_kind": "delivery"}),
        ("correction_followup_required", {"delivery_gate_kind": "correction"}),
        ("active", {}),
        ("halted", {}),
    ],
)
def test_attached_recovery_preserved_across_branches(
    fake_workspace, monkeypatch, condition, extra_fields,
):
    # Core attaches ``recovery`` to every classification; the projection must
    # carry it into ``recovery_lineage`` for ALL branches, not just the
    # source/closed/inert ones. A dropped nested recovery is a wire-field loss.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="running", project="/p/x", task="t",
            parent_run_id="20260101_000000",
        ),
    )

    def fake_run_diagnosis(run_id, *, cwd=None, meta=None, source_meta=None,
                           workspace=None, runs_dir=None):
        recovery = RecoveryLineage(
            run_id=run_id,
            is_terminal_or_rejected=False,
            continuation_subject="resume",
            recommended_next_action="resume_run",
            recommended_run_id=None,
            source_run_id=None,
            source_status=None,
            source_resumable=False,
            source_worktree_preserved=False,
            plan_subject_available=False,
            active_child_run_id=None,
            missing_facts=(),
            reason="attached recovery",
        )
        base = dict(
            run_id=run_id,
            condition=condition,
            reason="branch reason",
            status="running",
            halt_reason=None,
            continuation_subject=None,
            recommended_next_action=None,
            recommended_run_id=None,
            source_run_id=None,
            missing_facts=(),
            handoff_id=None,
            available_actions=(),
            delivery_gate_kind=None,
            blocked=False,
            block_message=None,
            recovery=recovery,
        )
        base.update(extra_fields)
        return RunDiagnosis(**base)

    monkeypatch.setattr(run_projection, "_sdk_run_diagnosis", fake_run_diagnosis)

    d = project_run_diagnosis("20260101_000001")

    assert d.recovery_lineage is not None, (
        f"branch {condition!r} dropped the core-attached recovery lineage"
    )
    assert d.recovery_lineage.continuation_subject == "resume"
    assert d.recovery_lineage.reason == "attached recovery"


@pytest.mark.parametrize(
    "condition, extra_fields",
    [
        # Core publishes ``recommended_run_id=run_id`` for needs_delivery_decision
        # and a plan-owning / source run for resume_inert_terminal; both must be
        # carried verbatim onto the wire projection (field-by-field parity).
        ("needs_delivery_decision", {"delivery_gate_kind": "delivery"}),
        (
            "resume_inert_terminal",
            {
                "continuation_subject": "plan_artifact",
                "recommended_next_action": "plan_artifact_continuation",
            },
        ),
    ],
)
def test_recommended_run_id_preserved_in_branch(
    fake_workspace, monkeypatch, condition, extra_fields,
):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="halted", project="/p/x", task="t"),
    )

    def fake_run_diagnosis(run_id, *, cwd=None, meta=None, source_meta=None,
                           workspace=None, runs_dir=None):
        recovery = RecoveryLineage(
            run_id=run_id,
            is_terminal_or_rejected=False,
            continuation_subject="resume",
            recommended_next_action="resume_run",
            recommended_run_id=None,
            source_run_id=None,
            source_status=None,
            source_resumable=False,
            source_worktree_preserved=False,
            plan_subject_available=False,
            active_child_run_id=None,
            missing_facts=(),
            reason="attached recovery",
        )
        base = dict(
            run_id=run_id,
            condition=condition,
            reason="branch reason",
            status="halted",
            halt_reason=None,
            continuation_subject=None,
            recommended_next_action=None,
            recommended_run_id="20260101_000099",
            source_run_id=None,
            missing_facts=(),
            handoff_id=None,
            available_actions=(),
            delivery_gate_kind=None,
            blocked=False,
            block_message=None,
            recovery=recovery,
        )
        base.update(extra_fields)
        return RunDiagnosis(**base)

    monkeypatch.setattr(run_projection, "_sdk_run_diagnosis", fake_run_diagnosis)

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == condition
    assert d.recommended_run_id == "20260101_000099", (
        f"branch {condition!r} dropped core's recommended_run_id"
    )


# ── Full field-by-field audit with MCP probes forced to None ─────────────────
#
# The parity contract: for EVERY branch, every field core's ``RunDiagnosis``
# publishes must reach ``RunDiagnosisProjection`` — INCLUDING the fallback paths
# where the MCP probes (delivery gate / pending handoff / follow-up lineage)
# return ``None``. A field that is only ever read from a probe is lost the moment
# the probe fails; this table pins that it falls back to core instead.


def _core_diagnosis(run_id: str, condition: str, **overrides) -> RunDiagnosis:
    """Build a core ``RunDiagnosis`` for ``condition`` with explicit overrides."""
    base = dict(
        run_id=run_id,
        condition=condition,
        reason="branch reason",
        status="halted",
        halt_reason=None,
        continuation_subject=None,
        recommended_next_action=None,
        recommended_run_id=None,
        source_run_id=None,
        missing_facts=(),
        handoff_id=None,
        available_actions=(),
        delivery_gate_kind=None,
        blocked=False,
        block_message=None,
        recovery=None,
    )
    base.update(overrides)
    return RunDiagnosis(**base)


@pytest.mark.parametrize(
    "condition, core_overrides, expected",
    [
        # needs_decision: handoff_id + available_actions are core-published.
        (
            "needs_decision",
            {"handoff_id": "h:1", "available_actions": ("continue", "halt")},
            {"handoff_id": "h:1", "available_actions": ["continue", "halt"]},
        ),
        # superseded_by_child: core publishes the live child's handoff_id and the
        # recommended_run_id — both must survive (handoff_id was the gap).
        (
            "superseded_by_child",
            {
                "recommended_run_id": "20260101_000002",
                "handoff_id": "implement:round:2",
                "continuation_subject": "active_child_run",
                "recommended_next_action": "resume_active_child",
            },
            {
                "recommended_run_id": "20260101_000002",
                "handoff_id": "implement:round:2",
                "recommended_action": "resume_child",
                "continuation_subject": "active_child_run",
                "recommended_next_action": "resume_active_child",
            },
        ),
        # blocked_worktree: recommended_run_id + blocked + block_message.
        (
            "blocked_worktree",
            {
                "recommended_run_id": "20260101_000001",
                "blocked": True,
                "block_message": "parent diff unavailable",
            },
            {
                "recommended_run_id": "20260101_000001",
                "blocked": True,
                "block_message": "parent diff unavailable",
            },
        ),
        # needs_delivery_decision with the gate probe DOWN: delivery_gate_kind
        # must fall back to core's bare kind mapped to the wire vocabulary, and
        # recommended_run_id/available_actions survive.
        (
            "needs_delivery_decision",
            {
                "delivery_gate_kind": "delivery",
                "recommended_run_id": "20260101_000001",
                "available_actions": ("commit", "halt"),
                "continuation_subject": "delivery_gate",
                "recommended_next_action": "delivery_decision",
            },
            {
                "delivery_gate_kind": "delivery_decision_required",
                "recommended_run_id": "20260101_000001",
                "available_actions": ["commit", "halt"],
                "continuation_subject": "delivery_gate",
                "recommended_next_action": "delivery_decision",
            },
        ),
        # correction_followup_required with the gate probe DOWN: same fallback to
        # the correction wire vocabulary; followup_* stay None without the gate.
        (
            "correction_followup_required",
            {
                "delivery_gate_kind": "correction",
                "available_actions": ("halt",),
                "continuation_subject": "plan_artifact",
                "recommended_next_action": "start_followup",
            },
            {
                "delivery_gate_kind": "correction_decision_required",
                "available_actions": ["halt"],
                "continuation_subject": "plan_artifact",
                "recommended_next_action": "start_followup",
                "followup_project_dir": None,
                "followup_diff_path": None,
                "followup_retained_worktree": None,
            },
        ),
        # recover_via_source_run: recommended_run_id + source_run_id.
        (
            "recover_via_source_run",
            {
                "recommended_run_id": "20260101_000001",
                "source_run_id": "20260101_000001",
                "continuation_subject": "source_run_checkpoint",
                "recommended_next_action": "resume_source_run",
            },
            {
                "recommended_run_id": "20260101_000001",
                "source_run_id": "20260101_000001",
                "continuation_subject": "source_run_checkpoint",
                "recommended_next_action": "resume_source_run",
            },
        ),
        # closed_by_followup: recommended_run_id (superseded child) + source_run_id.
        (
            "closed_by_followup",
            {
                "recommended_run_id": "20260101_000002",
                "source_run_id": "20260101_000000",
                "continuation_subject": "none",
            },
            {
                "recommended_run_id": "20260101_000002",
                "source_run_id": "20260101_000000",
                "continuation_subject": "none",
            },
        ),
        # resume_inert_terminal: recommended_run_id + source_run_id + missing_facts.
        (
            "resume_inert_terminal",
            {
                "recommended_run_id": "20260101_000001",
                "source_run_id": "20260101_000001",
                "missing_facts": ("no active child",),
                "continuation_subject": "plan_artifact",
                "recommended_next_action": "plan_artifact_continuation",
            },
            {
                "recommended_run_id": "20260101_000001",
                "source_run_id": "20260101_000001",
                "missing_facts": ["no active child"],
                "continuation_subject": "plan_artifact",
                "recommended_next_action": "plan_artifact_continuation",
            },
        ),
        # active: only the base fields are published.
        (
            "active",
            {"status": "running"},
            {"status": "running"},
        ),
        # residual resumable: core sets continuation_subject='none', but the wire
        # projection INTENTIONALLY leaves it None (pre-migration compat — a
        # resumable run continues itself). This pins that documented divergence.
        (
            "halted",
            {"continuation_subject": "none"},
            {"continuation_subject": None},
        ),
    ],
)
def test_field_by_field_parity_with_probes_down(
    fake_workspace, monkeypatch, condition, core_overrides, expected,
):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="halted", project="/p/x", task="t"),
    )

    def fake_run_diagnosis(run_id, *, cwd=None, meta=None, source_meta=None,
                           workspace=None, runs_dir=None):
        return _core_diagnosis(run_id, condition, **core_overrides)

    # All MCP enrichment probes are DOWN: every published core field must still
    # reach the wire projection from core, not from a probe.
    monkeypatch.setattr(run_projection, "_sdk_run_diagnosis", fake_run_diagnosis)
    monkeypatch.setattr(run_projection, "_safe_delivery_gate", lambda _r: None)
    monkeypatch.setattr(run_projection, "_safe_pending_handoff", lambda _r: None)
    monkeypatch.setattr(run_projection, "_safe_followup_lineage", lambda _r: None)

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == condition
    for field_name, want in expected.items():
        got = getattr(d, field_name)
        assert got == want, (
            f"branch {condition!r} field {field_name!r}: "
            f"expected {want!r}, got {got!r}"
        )


def test_available_actions_core_tuple_becomes_wire_list(fake_workspace, monkeypatch):
    # A core ``available_actions`` tuple is projected to a wire list.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x", task="t",
            phase_handoff={"id": "h:1", "available_actions": ["continue", "halt"]},
        ),
    )

    def fake_run_diagnosis(run_id, *, cwd=None, meta=None, source_meta=None,
                           workspace=None, runs_dir=None):
        return RunDiagnosis(
            run_id=run_id,
            condition="needs_decision",
            reason="paused",
            status="awaiting_phase_handoff",
            halt_reason=None,
            continuation_subject=None,
            recommended_next_action=None,
            recommended_run_id=None,
            source_run_id=None,
            missing_facts=(),
            handoff_id="h:1",
            available_actions=("continue", "halt"),
            delivery_gate_kind=None,
            blocked=False,
            block_message=None,
            recovery=None,
        )

    monkeypatch.setattr(run_projection, "_sdk_run_diagnosis", fake_run_diagnosis)

    d = project_run_diagnosis("20260101_000001")

    assert d.available_actions == ["continue", "halt"]
    assert isinstance(d.available_actions, list)
    assert d.handoff_id == "h:1"


def test_source_meta_seeds_supervisor_merged_status_diagnosis(fake_workspace):
    # Seedback contract (mirrors T1): the source's on-disk status is a stale
    # ``running`` but the supervisor settled it terminal-success. The merged
    # status must flow into core's ``source_meta`` seam, so the terminal source
    # is NOT a resumable checkpoint — the recovery child is an inert dead-end,
    # NOT recover_via_source_run.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="running", project="/p/x", task="source",
            worktree=_RETAINED_WORKTREE,
        ),
        supervisor_state=supervisor_state(
            run_id="20260101_000001", status="done",
        ),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=meta(
            status="halted", project="/p/x", task="recovery",
            halt_reason="phase_handoff_halt",
            resume_mode="followup", parent_run_id="20260101_000001",
        ),
    )

    d = project_run_diagnosis("20260101_000002")

    assert d.condition != "recover_via_source_run"
    assert d.condition == "resume_inert_terminal"
    assert d.continuation_subject == "unknown"
    assert d.recommended_next_action == "stop_unknown"


# ── controllability axis (T2): control / control_reason overlay ──────────────
#
# ``control`` is orthogonal to ``condition``: a run started by THIS MCP server
# (durable mcp_supervisor.json with a project_dir) is ``mcp_controllable``; a
# foreign / CLI run dir (meta.json only) is ``inspect_only``. The overlay is a
# single point in ``project_run_diagnosis`` and must reach both the projection
# and the ``orcho_run_diagnose`` wire model.


def test_control_inspect_only_for_foreign_run_dir(fake_workspace):
    # Foreign / CLI run: meta.json only, no mcp_supervisor.json. A resumable
    # condition is still classified inspect_only on the control axis.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="failed", project="/p/x", task="t"),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "failed"
    assert d.control == "inspect_only"
    assert d.control_reason and "no mcp_supervisor.json" in d.control_reason


def test_control_mcp_controllable_for_mcp_started_run(fake_workspace):
    # MCP-started run: durable mcp_supervisor.json with a project_dir.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="failed", project="/p/x", task="t"),
        supervisor_state=supervisor_state(
            run_id="20260101_000001", status="failed", project_dir="/p/x",
        ),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "failed"
    assert d.control == "mcp_controllable"
    assert d.control_reason and "project_dir=/p/x" in d.control_reason


def test_diagnose_tool_carries_control_for_both_run_dirs(fake_workspace):
    # Wire check: RunDiagnosis.control is set on both a foreign and an
    # MCP-started run dir, with a non-empty control_reason.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="failed", project="/p/x", task="t"),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=meta(status="failed", project="/p/x", task="t"),
        supervisor_state=supervisor_state(
            run_id="20260101_000002", status="failed", project_dir="/p/x",
        ),
    )

    foreign = orcho_run_diagnose("20260101_000001")
    assert foreign.control == "inspect_only"
    assert foreign.control_reason

    mcp_started = orcho_run_diagnose("20260101_000002")
    assert mcp_started.control == "mcp_controllable"
    assert mcp_started.control_reason


def test_needs_delivery_decision_kind_is_wire_vocab(fake_workspace):
    # The wire ``delivery_gate_kind`` keeps the MCP vocabulary
    # (``delivery_decision_required``), not core's bare ``delivery`` kind.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="halted", project="/p/x", task="t",
            halt_reason="commit_delivery_pending",
            commit_delivery=_commit_delivery(
                status="pending", release_verdict="APPROVED", action="approve",
            ),
        ),
    )

    d = project_run_diagnosis("20260101_000001")

    assert d.condition == "needs_delivery_decision"
    assert d.delivery_gate_kind == "delivery_decision_required"
