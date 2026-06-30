"""Durable recovery-lineage projection (T1).

``project_recovery_lineage`` is the single resolver shared by
``orcho_run_diagnose`` and ``orcho_run_status`` for the question "what is the
safe continuation subject of this run?". These tests pin the four
continuation cases plus the defensive degrade-to-unknown behaviour, all from
synthetic ``meta.json`` fixtures (no log scraping, no SDK pipeline):

- Case A — a terminal / rejected recovery child pointing (via ``parent_run_id``
  or ``plan_source_run_id``) at a resumable source recommends resuming the
  *source*, never a fresh ``from_run_plan``;
- Case B — an active follow-up child supersedes the inspected run;
- Case C — a plan-only / research run with a persisted plan recommends a fresh
  implementation run from the plan artifact;
- Case D — a terminal dead-end with no durable continuation fact stops with an
  explicit ``missing_facts`` list.

The dogfood shapes mirror the real incident: a failed source with a retained
worktree (analog ``fc2da4``) and a terminal recovery child pointing back at it
(analog ``474b85``).
"""
from __future__ import annotations

from sdk.run_control import RecoveryLineage

from orcho_mcp.services import run_lineage
from orcho_mcp.services.run_lineage import (
    ContinuationSubject,
    RecommendedNextAction,
    project_recovery_lineage,
)
from tests.fixtures.mcp_workspace import meta, supervisor_state, write_run

_RETAINED_WORKTREE = {"isolation": "worktree", "path": "/tmp/wt/source"}
# Minimal durable parsed_plan.json body — a plan-only subject requires the
# artifact to actually exist on disk, not just a meta.plan_source stamp.
_PARSED_PLAN = {"tasks": [{"id": "T1", "spec": "do the thing"}]}


def _source_failed_with_worktree(**extra):
    return meta(
        status="failed", project="/p/x", task="source task",
        worktree=_RETAINED_WORKTREE, **extra,
    )


# ── Case A — terminal/rejected recovery child → resume source ────────────────


def test_case_a_via_parent_run_id_recommends_source(fake_workspace):
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

    rec = project_recovery_lineage("20260101_000002")

    assert rec.is_terminal_or_rejected is True
    assert rec.continuation_subject == ContinuationSubject.SOURCE_RUN_CHECKPOINT
    assert rec.recommended_next_action == RecommendedNextAction.RESUME_SOURCE_RUN
    assert rec.recommended_run_id == "20260101_000001"
    assert rec.source_run_id == "20260101_000001"
    assert rec.source_status == "failed"
    assert rec.source_resumable is True
    assert rec.source_worktree_preserved is True
    assert rec.missing_facts == []
    assert rec.reason


def test_case_a_via_plan_source_run_id_recommends_source(fake_workspace):
    # No parent_run_id: the durable pointer is plan_source_run_id (a
    # from_run_plan recovery child). The source is still resumable, so resume
    # the source rather than start a fresh from_run_plan implementation.
    write_run(
        fake_workspace, "20260101_000001",
        meta=_source_failed_with_worktree(),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=meta(
            status="halted", project="/p/x", task="recovery",
            halt_reason="commit_decision_halt",
            plan_source="run", plan_source_run_id="20260101_000001",
        ),
    )

    rec = project_recovery_lineage("20260101_000002")

    assert rec.continuation_subject == ContinuationSubject.SOURCE_RUN_CHECKPOINT
    assert rec.recommended_next_action == RecommendedNextAction.RESUME_SOURCE_RUN
    assert rec.recommended_run_id == "20260101_000001"
    assert rec.source_resumable is True


def test_case_a_source_resumable_via_persisted_plan_not_worktree(fake_workspace):
    # A source with no retained worktree but a persisted plan is still
    # resumable (retained work = preserved worktree OR plan_source != none).
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="interrupted", project="/p/x", task="source",
            plan_source="local",
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

    rec = project_recovery_lineage("20260101_000002")

    assert rec.continuation_subject == ContinuationSubject.SOURCE_RUN_CHECKPOINT
    assert rec.source_resumable is True
    assert rec.source_worktree_preserved is False


def test_case_a_terminal_source_is_not_resumable_falls_to_unknown(fake_workspace):
    # A source that is itself terminal (done) is NOT a resumable continuation
    # subject; with no other durable fact the recovery child stops at unknown.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="source"),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=meta(
            status="halted", project="/p/x", task="recovery",
            halt_reason="phase_handoff_halt",
            resume_mode="followup", parent_run_id="20260101_000001",
        ),
    )

    rec = project_recovery_lineage("20260101_000002")

    assert rec.continuation_subject == ContinuationSubject.UNKNOWN
    assert rec.recommended_next_action == RecommendedNextAction.STOP_UNKNOWN
    # The source pointer is still reported for diagnostics, but not resumable.
    assert rec.source_run_id == "20260101_000001"
    assert rec.source_resumable is False
    assert "no source/parent run id" in rec.missing_facts


# ── Case B — active follow-up child supersedes the inspected run ─────────────


def test_case_b_active_child_supersedes_parent(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=meta(
            status="running", project="/p/x", task="follow-up",
            resume_mode="followup", parent_run_id="20260101_000001",
        ),
    )

    rec = project_recovery_lineage("20260101_000001")

    assert rec.continuation_subject == ContinuationSubject.ACTIVE_CHILD_RUN
    assert rec.recommended_next_action == RecommendedNextAction.RESUME_ACTIVE_CHILD
    assert rec.recommended_run_id == "20260101_000002"
    assert rec.active_child_run_id == "20260101_000002"


def test_case_b_active_child_outranks_source(fake_workspace):
    # Inspected run is a terminal recovery child of a resumable source AND has
    # its own active follow-up child: the active child wins (priority 1).
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
    write_run(
        fake_workspace, "20260101_000003",
        meta=meta(
            status="running", project="/p/x", task="grandchild",
            resume_mode="followup", parent_run_id="20260101_000002",
        ),
    )

    rec = project_recovery_lineage("20260101_000002")

    assert rec.continuation_subject == ContinuationSubject.ACTIVE_CHILD_RUN
    assert rec.recommended_run_id == "20260101_000003"


# ── Case C — plan-only / research subject ────────────────────────────────────


def test_case_c_plan_only_parent_recommends_plan_continuation(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="done", project="/p/x", task="plan it",
            profile="planning", plan_source="local",
        ),
        parsed_plan=_PARSED_PLAN,
    )

    rec = project_recovery_lineage("20260101_000001")

    assert rec.continuation_subject == ContinuationSubject.PLAN_ARTIFACT
    assert rec.recommended_next_action == (
        RecommendedNextAction.PLAN_ARTIFACT_CONTINUATION
    )
    assert rec.recommended_run_id == "20260101_000001"
    assert rec.plan_subject_available is True


def test_case_c_research_profile_with_clean_head_followup(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="done", project="/p/x", task="research",
            profile="research", plan_source="cross",
            worktree={
                "isolation": "worktree",
                "path": "/tmp/wt/child",
                "followup_continuity": {
                    "blocked": False,
                    "diff_source": "none",
                    "reason": "parent had no undelivered diff",
                    "mode_label": "clean_head_no_undelivered_diff",
                },
            },
        ),
        parsed_plan=_PARSED_PLAN,
    )

    rec = project_recovery_lineage("20260101_000001")

    assert rec.continuation_subject == ContinuationSubject.PLAN_ARTIFACT
    assert rec.plan_subject_available is True


def test_case_c_plan_source_without_parsed_plan_artifact_is_unknown(fake_workspace):
    # F4 guard: a plan-only profile + meta.plan_source stamp but NO durable
    # parsed_plan.json artifact must NOT recommend plan-artifact continuation —
    # the resolver never guesses a plan it cannot read.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="done", project="/p/x", task="plan it",
            profile="planning", plan_source="local",
        ),
        # no parsed_plan written
    )

    rec = project_recovery_lineage("20260101_000001")

    assert rec.plan_subject_available is False
    assert rec.continuation_subject != ContinuationSubject.PLAN_ARTIFACT


def test_case_c_corrupt_parsed_plan_artifact_is_unknown(fake_workspace):
    # A corrupt parsed_plan.json degrades to "no plan artifact" rather than
    # raising or being treated as a durable plan subject.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="done", project="/p/x", task="plan it",
            profile="planning", plan_source="local",
        ),
        parsed_plan_text="{ this is not valid json",
    )

    rec = project_recovery_lineage("20260101_000001")

    assert rec.plan_subject_available is False
    assert rec.continuation_subject != ContinuationSubject.PLAN_ARTIFACT


def test_case_c_not_plan_only_when_worktree_retains_diff(fake_workspace):
    # A persisted plan + a retained worktree (undelivered diff) is NOT a
    # plan-only subject — from_run_plan must stay narrow.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="done", project="/p/x", task="t",
            profile="planning", plan_source="local",
            worktree=_RETAINED_WORKTREE,
        ),
        parsed_plan=_PARSED_PLAN,
    )

    rec = project_recovery_lineage("20260101_000001")

    assert rec.plan_subject_available is False
    assert rec.continuation_subject != ContinuationSubject.PLAN_ARTIFACT


def test_case_c_not_plan_only_for_non_planning_profile(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="done", project="/p/x", task="t",
            profile="feature", plan_source="local",
        ),
        parsed_plan=_PARSED_PLAN,
    )

    rec = project_recovery_lineage("20260101_000001")

    assert rec.plan_subject_available is False
    assert rec.continuation_subject != ContinuationSubject.PLAN_ARTIFACT


# ── Case D — terminal dead-end with no durable continuation fact ─────────────


def test_case_d_terminal_deadend_stops_unknown_with_missing_facts(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="halted", project="/p/x", task="t",
            halt_reason="phase_handoff_halt", profile="feature",
        ),
    )

    rec = project_recovery_lineage("20260101_000001")

    assert rec.is_terminal_or_rejected is True
    assert rec.continuation_subject == ContinuationSubject.UNKNOWN
    assert rec.recommended_next_action == RecommendedNextAction.STOP_UNKNOWN
    assert rec.recommended_run_id is None
    assert set(rec.missing_facts) == {
        "no source/parent run id",
        "no plan artifact",
        "no delivery gate",
        "no active child",
    }
    # Never offer from_run_plan as a generic fallback.
    assert rec.plan_subject_available is False


def test_case_d_unknown_source_meta_degrades_not_raises(fake_workspace):
    # Recovery child points at a parent that does not exist on disk: the source
    # read degrades to unknown with an explicit missing fact, never raising.
    write_run(
        fake_workspace, "20260101_000002",
        meta=meta(
            status="halted", project="/p/x", task="recovery",
            halt_reason="phase_handoff_halt",
            resume_mode="followup", parent_run_id="20260101_000001",
        ),
    )

    rec = project_recovery_lineage("20260101_000002")

    assert rec.continuation_subject == ContinuationSubject.UNKNOWN
    assert rec.recommended_next_action == RecommendedNextAction.STOP_UNKNOWN
    assert "no source/parent run id" in rec.missing_facts


def test_missing_inspected_run_degrades_to_unknown(fake_workspace):
    # A missing inspected run never raises out of the resolver — it degrades to
    # a typed unknown so diagnose/status callers can compose it defensively.
    rec = project_recovery_lineage("20260101_999999")

    assert rec.continuation_subject == ContinuationSubject.UNKNOWN
    assert rec.recommended_next_action == RecommendedNextAction.STOP_UNKNOWN
    assert rec.missing_facts
    assert "could not read run meta" in rec.reason


# ── Regression — normal resumable failed-child resumes itself ────────────────


def test_resumable_failed_child_with_resumable_parent_continues_itself(
    fake_workspace,
):
    # The Case B regression guard: an ordinary resumable failed child with a
    # resumable parent is NOT a terminal dead-end, so the resolver adds no
    # source recovery — the diagnosis keeps recommending the child itself.
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

    rec = project_recovery_lineage("20260101_000002")

    assert rec.is_terminal_or_rejected is False
    assert rec.continuation_subject == ContinuationSubject.NONE
    assert rec.recommended_next_action is None
    # Source linkage is still reported, but no recovery is recommended.
    assert rec.source_run_id == "20260101_000001"
    assert rec.source_resumable is True


def test_clean_terminal_success_recommends_start_followup(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t", profile="feature"),
    )

    rec = project_recovery_lineage("20260101_000001")

    assert rec.is_terminal_or_rejected is True
    assert rec.continuation_subject == ContinuationSubject.NONE
    assert rec.recommended_next_action == RecommendedNextAction.START_FOLLOWUP


# ── Migration contract — thin projection of core ``recovery_lineage`` ────────


def test_maps_core_recovery_lineage_field_for_field(fake_workspace, monkeypatch):
    # The projection is a field-for-field mapper over core's typed read-model:
    # every published field is carried verbatim and ``missing_facts`` is
    # converted tuple → list. Core is stubbed so the mapping is asserted in
    # isolation from core's resolution.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="failed", project="/p/x", task="t"),
    )
    captured: dict = {}

    def fake_recovery_lineage(run_id, *, cwd=None, meta=None, source_meta=None,
                              workspace=None, runs_dir=None):
        captured["run_id"] = run_id
        captured["cwd"] = cwd
        captured["meta"] = meta
        captured["source_meta"] = source_meta
        return RecoveryLineage(
            run_id="20260101_000001",
            is_terminal_or_rejected=True,
            continuation_subject="source_run_checkpoint",
            recommended_next_action="resume_source_run",
            recommended_run_id="src",
            source_run_id="src",
            source_status="failed",
            source_resumable=True,
            source_worktree_preserved=True,
            plan_subject_available=False,
            active_child_run_id=None,
            missing_facts=("no plan artifact", "no active child"),
            reason="fact-built reason",
        )

    monkeypatch.setattr(run_lineage, "recovery_lineage", fake_recovery_lineage)

    rec = project_recovery_lineage("20260101_000001")

    # Field-for-field parity with the typed core read-model.
    assert rec.run_id == "20260101_000001"
    assert rec.is_terminal_or_rejected is True
    assert rec.continuation_subject == ContinuationSubject.SOURCE_RUN_CHECKPOINT
    assert rec.recommended_next_action == RecommendedNextAction.RESUME_SOURCE_RUN
    assert rec.recommended_run_id == "src"
    assert rec.source_run_id == "src"
    assert rec.source_status == "failed"
    assert rec.source_resumable is True
    assert rec.source_worktree_preserved is True
    assert rec.plan_subject_available is False
    assert rec.active_child_run_id is None
    assert rec.reason == "fact-built reason"
    # ``missing_facts``: core tuple → wire list (same values, same order).
    assert rec.missing_facts == ["no plan artifact", "no active child"]
    assert isinstance(rec.missing_facts, list)
    # Core is fed walk-up-disabled resolution + supervisor-merged inspected meta.
    assert captured["cwd"] is None
    assert captured["meta"]["status"] == "failed"


def test_core_call_error_degrades_to_unknown_not_raises(fake_workspace, monkeypatch):
    # Defensive contract: a core ``recovery_lineage`` that raises (e.g. an
    # invalid run id surfacing a ``ValueError``) must degrade to the typed
    # unknown / stop_unknown dead-end with a fact-built reason and the full
    # missing_facts set, never propagate into orcho_run_status.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="failed", project="/p/x", task="t"),
    )

    def boom(*_args, **_kwargs):
        raise ValueError("run_id must not be empty")

    monkeypatch.setattr(run_lineage, "recovery_lineage", boom)

    rec = project_recovery_lineage("20260101_000001")

    assert rec.continuation_subject == ContinuationSubject.UNKNOWN
    assert rec.recommended_next_action == RecommendedNextAction.STOP_UNKNOWN
    assert rec.is_terminal_or_rejected is False
    assert rec.recommended_run_id is None
    assert set(rec.missing_facts) == {
        "no source/parent run id",
        "no plan artifact",
        "no delivery gate",
        "no active child",
    }
    assert "could not classify recovery lineage" in rec.reason
    assert "ValueError" in rec.reason


def test_mapper_error_degrades_to_unknown_not_raises(fake_workspace, monkeypatch):
    # A malformed core read-model that breaks the field mapper must also degrade
    # to unknown rather than propagate — the never-raise contract covers the
    # mapping step, not just the core call.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="failed", project="/p/x", task="t"),
    )

    class _Broken:
        # Accessing any field raises, simulating a corrupt/partial core object.
        def __getattr__(self, name):
            raise AttributeError(name)

    monkeypatch.setattr(
        run_lineage, "recovery_lineage", lambda *a, **k: _Broken(),
    )

    rec = project_recovery_lineage("20260101_000001")

    assert rec.continuation_subject == ContinuationSubject.UNKNOWN
    assert rec.recommended_next_action == RecommendedNextAction.STOP_UNKNOWN
    assert "could not classify recovery lineage" in rec.reason


def test_source_meta_seeds_supervisor_merged_status(fake_workspace):
    # Seedback contract: the source's on-disk status is a stale ``running`` (a
    # SIGKILL bypassed the meta writer) but the supervisor settled it
    # terminal-success. The supervisor-merged status must flow into core's
    # ``source_meta`` seam, so the terminal source is NOT mistaken for a
    # resumable checkpoint and the recovery child falls to a known dead-end.
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

    rec = project_recovery_lineage("20260101_000002")

    assert rec.source_run_id == "20260101_000001"
    # Supervisor-merged status, not the stale on-disk ``running``.
    assert rec.source_status == "done"
    assert rec.source_resumable is False
    assert rec.continuation_subject == ContinuationSubject.UNKNOWN
