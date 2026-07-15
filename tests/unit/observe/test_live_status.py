"""L1 unit tests for ``orcho_run_live_status``.

Calls the ``@mcp.tool`` handler as a plain Python function against
synthetic run state from ``tests/fixtures/mcp_workspace.py``. Covers the
six mandatory live-status scenarios — running phase, running subtask,
awaiting phase-handoff, clean terminal success, the legacy inconsistent
terminal (``done`` + ``final_acceptance`` reject), and a halted run whose
release was rejected — plus the bounded-payload guarantee.

These tests are deterministic and offline: no network, no subprocess,
no real orcho install. ``fake_workspace`` points orcho at a temp tree
and ``write_run`` lays down the ``meta.json`` / ``events.jsonl`` each
scenario needs.
"""
from __future__ import annotations

from sdk import phase_handoff_decide

from orcho_mcp.observe.live_status import _resume_meaningful_from_diagnosis
from orcho_mcp.services.run_projection import project_run_diagnosis
from orcho_mcp.tools import orcho_run_live_status
from tests.fixtures.mcp_workspace import event, meta, write_run


def _final_acceptance(verdict: str, approved: bool, **extra) -> dict:
    """Build a ``meta.phases.final_acceptance`` gate block.

    Mirrors the persisted shape the projection reads
    (``verdict`` + the ``approved`` bool companion). ``short_summary`` is
    a tiny preview field tests can assert stays bounded.
    """
    out = {"verdict": verdict, "approved": approved}
    out.update(extra)
    return out


# ── (1) running phase ────────────────────────────────────────────────────────

def test_running_phase(fake_workspace):
    """A run mid-phase with no subtask classifies as ``running_phase`` and
    surfaces run_id / status / current_phase / last_activity / next_action."""
    write_run(
        fake_workspace, "run_running_phase",
        meta=meta(status="running", project="/p/x", task="ship it"),
        events=[
            event(1, "run.start"),
            event(2, "phase.start", phase="implement",
                  payload={"summary": "starting implement"}),
            event(3, "agent.text", phase="implement",
                  payload={"text": "writing the patch"}),
        ],
    )

    card = orcho_run_live_status("run_running_phase")

    assert card.run_id == "run_running_phase"
    assert card.status == "running"
    assert card.state_class == "running_phase"
    assert card.current_phase == "implement"
    assert card.current_subtask is None
    # last_activity mirrors the most recent event.
    assert card.last_activity is not None
    assert card.last_activity.kind == "agent.text"
    assert card.last_activity.phase == "implement"
    assert card.last_activity.preview == "writing the patch"
    # next_action is a conservative poll pointer; terminal/handoff slices absent.
    assert card.next_action
    assert "poll" in card.next_action.lower()
    assert card.pending_handoff is None
    assert card.terminal is None
    assert card.consistency_flags == []
    assert card.next_seq == 3


# ── (2) running implement subtask ────────────────────────────────────────────

def test_running_subtask(fake_workspace):
    """An in-flight subtask_dag subtask classifies as ``running_subtask`` and
    carries subtask_id / index / total / goal / state."""
    write_run(
        fake_workspace, "run_running_subtask",
        meta=meta(status="running", project="/p/x", task="t"),
        events=[
            event(1, "phase.start", phase="implement"),
            event(2, "subtask.start", phase="implement", payload={
                "subtask_id": "T3",
                "index": 3,
                "total": 12,
                "goal": "Patch the target module",
            }),
        ],
    )

    card = orcho_run_live_status("run_running_subtask")

    assert card.state_class == "running_subtask"
    assert card.current_phase == "implement"
    sub = card.current_subtask
    assert sub is not None
    assert sub.subtask_id == "T3"
    assert sub.index == 3
    assert sub.total == 12
    assert sub.goal == "Patch the target module"
    assert sub.state == "running"


# ── (3) awaiting phase-handoff ───────────────────────────────────────────────

def test_awaiting_phase_handoff(fake_workspace):
    """A paused run classifies as ``awaiting_handoff`` and exposes the
    handoff id / phase / available_actions / default_action / verdict /
    findings_summary / recommended action — reusing the existing
    projections (no re-parse of meta.phase_handoff in the card)."""
    write_run(
        fake_workspace, "run_awaiting_handoff",
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x", task="t",
            phase_handoff={
                "id": "h-42",
                "phase": "review_changes",
                "trigger": "rejected",
                "verdict": "REJECTED",
                "available_actions": ["retry_feedback", "continue", "halt"],
                "findings": [
                    {"severity": "P1", "title": "Null deref in handler"},
                ],
            },
        ),
        events=[event(1, "phase.start", phase="review_changes")],
    )

    card = orcho_run_live_status("run_awaiting_handoff")

    assert card.state_class == "awaiting_handoff"
    assert card.terminal is None
    h = card.pending_handoff
    assert h is not None
    assert h.handoff_id == "h-42"
    assert h.phase == "review_changes"
    assert h.available_actions == ["retry_feedback", "continue", "halt"]
    # default_action heuristic prefers retry_feedback after a reject.
    assert h.default_action == "retry_feedback"
    assert h.verdict == "REJECTED"
    # findings_summary is built from the compact reviewer findings.
    assert h.findings_summary is not None
    assert "Null deref in handler" in h.findings_summary
    # recommended action points at decide-then-resume.
    assert h.recommended_action
    assert "orcho_phase_handoff_decide" in h.recommended_action
    # next_action mirrors the handoff recommendation.
    assert card.next_action == h.recommended_action


# ── (3b) awaiting handoff WITH a recorded decision → resume ───────────────────

def test_awaiting_handoff_with_recorded_decision_points_to_resume(fake_workspace):
    """Coherence with ``orcho_run_diagnose``: once a decision artifact exists
    the run stays ``awaiting_phase_handoff`` (continue does not flip status),
    but both ``pending_handoff.recommended_action`` and the card's
    ``next_action`` route the captain at ``orcho_run_resume`` — not a second
    ``orcho_phase_handoff_decide``."""
    write_run(
        fake_workspace, "run_handoff_decided",
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x", task="t",
            phase_handoff={
                "id": "h-7",
                "phase": "validate_plan",
                "available_actions": ["continue", "retry_feedback", "halt"],
            },
        ),
        events=[event(1, "phase.start", phase="validate_plan")],
    )
    phase_handoff_decide("run_handoff_decided", "h-7", "continue", cwd=None)

    card = orcho_run_live_status("run_handoff_decided")

    assert card.state_class == "awaiting_handoff"
    h = card.pending_handoff
    assert h is not None
    # Decision recorded → recommended_action points at resume, not decide.
    assert "orcho_run_resume" in h.recommended_action
    assert "orcho_phase_handoff_decide" not in h.recommended_action
    # The card-level next_action mirrors the same resume recommendation.
    assert card.next_action == h.recommended_action


# ── (4) clean terminal success ───────────────────────────────────────────────

def test_terminal_success(fake_workspace):
    """A clean ``done`` run with an APPROVED final_acceptance yields a
    coherent terminal card: resume_meaningful=False, no inconsistency flags."""
    write_run(
        fake_workspace, "run_terminal_success",
        meta=meta(
            status="done", project="/p/x", task="t",
            phases={"final_acceptance": _final_acceptance("APPROVED", True)},
        ),
        events=[event(1, "run.end", payload={"status": "done"})],
    )

    card = orcho_run_live_status("run_terminal_success")

    assert card.state_class == "terminal_success"
    assert card.pending_handoff is None
    term = card.terminal
    assert term is not None
    assert term.final_acceptance == "APPROVED"
    assert term.final_acceptance_rejected is False
    assert term.resume_meaningful is False
    assert term.inconsistencies == []
    assert card.consistency_flags == []


# ── (5) legacy inconsistent terminal (done + reject) ─────────────────────────

def test_terminal_inconsistent_done_but_rejected(fake_workspace):
    """A ``done`` status alongside a REJECTED final_acceptance is a
    contradiction: state_class=terminal_inconsistent and an explicit flag in
    consistency_flags — the contradiction is surfaced, never hidden."""
    write_run(
        fake_workspace, "run_terminal_inconsistent",
        meta=meta(
            status="done", project="/p/x", task="t",
            phases={"final_acceptance": _final_acceptance("REJECTED", False)},
        ),
        events=[event(1, "run.end", payload={"status": "done"})],
    )

    card = orcho_run_live_status("run_terminal_inconsistent")

    assert card.state_class == "terminal_inconsistent"
    # The contradiction is surfaced explicitly at the top level...
    assert card.consistency_flags
    assert any("final_acceptance_rejected" in f for f in card.consistency_flags)
    # ...and inside the terminal card.
    term = card.terminal
    assert term is not None
    assert term.final_acceptance == "REJECTED"
    assert term.final_acceptance_rejected is True
    assert term.inconsistencies == card.consistency_flags


# ── (6) halted + final_acceptance_rejected (rejected terminal dead-end) ───────

def test_terminal_halted_final_acceptance_rejected(fake_workspace):
    """A run halted on ``final_acceptance_rejected`` is a terminal dead-end
    (T1 vocabulary): the card surfaces the halt_reason + rejection flag, but
    ``resume_meaningful`` is False and ``next_action`` points at evidence
    only — never ``orcho_run_resume`` — so the live card never advertises a
    resume the pre-flight / diagnose would block. Shape mirrors run
    20260626_165338_90fb22 (halted + final_acceptance_rejected, no
    phase_handoff / delivery gate / parent)."""
    write_run(
        fake_workspace, "run_terminal_rejected",
        meta=meta(
            status="halted", project="/p/x", task="t",
            halt_reason="final_acceptance_rejected",
            phases={"final_acceptance": _final_acceptance("REJECTED", False)},
        ),
        events=[event(1, "run.end", payload={"status": "halted"})],
    )

    card = orcho_run_live_status("run_terminal_rejected")

    assert card.state_class == "terminal_halted"
    term = card.terminal
    assert term is not None
    assert term.halt_reason == "final_acceptance_rejected"
    assert term.final_acceptance_rejected is True
    assert term.final_acceptance == "REJECTED"
    # Rejected terminal dead-end: resume is inert; the card must not advertise
    # one (criterion 1).
    assert term.resume_meaningful is False
    assert "orcho_run_resume" not in card.next_action
    # Coherent (it halted *because* of the rejection), so no contradiction flag.
    assert term.inconsistencies == []
    assert card.consistency_flags == []


def test_terminal_halted_phase_handoff_halt_is_resume_inert(fake_workspace):
    """A ``phase_handoff_halt`` terminal is equally a dead-end under the
    unified semantics: ``resume_meaningful`` is False and ``next_action``
    omits ``orcho_run_resume`` — the live card and the resume pre-flight agree
    that resuming a halted-by-operator run is inert."""
    write_run(
        fake_workspace, "run_terminal_handoff_halt",
        meta=meta(
            status="halted", project="/p/x", task="t",
            halt_reason="phase_handoff_halt",
        ),
        events=[event(1, "run.end", payload={"status": "halted"})],
    )

    card = orcho_run_live_status("run_terminal_handoff_halt")

    assert card.state_class == "terminal_halted"
    assert card.terminal is not None
    assert card.terminal.resume_meaningful is False
    assert "orcho_run_resume" not in card.next_action


def test_terminal_halted_residual_resumable(fake_workspace):
    """A halted run whose halt_reason is NOT terminal (e.g.
    ``pre_run_dirty_halt``) is residual-resumable: ``resume_meaningful`` is
    True and ``next_action`` offers ``orcho_run_resume`` — the positive
    control that the unified mapping still lets a genuinely resumable halt
    advertise resume."""
    write_run(
        fake_workspace, "run_residual_halted",
        meta=meta(
            status="halted", project="/p/x", task="t",
            halt_reason="pre_run_dirty_halt",
        ),
        events=[event(1, "run.end", payload={"status": "halted"})],
    )

    card = orcho_run_live_status("run_residual_halted")

    assert card.state_class == "terminal_halted"
    assert card.terminal is not None
    assert card.terminal.resume_meaningful is True
    assert "orcho_run_resume" in card.next_action


# ── (6b) resume_meaningful ↔ diagnose.condition consistency regress ───────────

def test_resume_meaningful_matches_diagnosis_unresolved_handoff(fake_workspace):
    """Unresolved ``awaiting_phase_handoff``: diagnosis is ``needs_decision``
    with no recorded artifact, so the unified mapping yields
    ``resume_meaningful=False`` — yet the live card STILL surfaces the
    pending handoff with its available_actions, and ``next_action`` points at
    decide (not plain resume). A decision surface is never collapsed into a
    meaningful plain resume (criterion 3 preserved)."""
    write_run(
        fake_workspace, "run_unresolved_handoff",
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x", task="t",
            phase_handoff={
                "id": "h-9",
                "phase": "review_changes",
                "verdict": "REJECTED",
                "available_actions": ["retry_feedback", "continue", "halt"],
            },
        ),
        events=[event(1, "phase.start", phase="review_changes")],
    )

    diag = project_run_diagnosis("run_unresolved_handoff")
    assert diag.condition == "needs_decision"
    assert diag.decision_artifact_exists is False
    assert _resume_meaningful_from_diagnosis(diag) is False

    card = orcho_run_live_status("run_unresolved_handoff")
    # pending_handoff is surfaced with its actions; next_action is decide.
    assert card.pending_handoff is not None
    assert card.pending_handoff.available_actions == [
        "retry_feedback", "continue", "halt",
    ]
    assert "orcho_phase_handoff_decide" in card.next_action


def test_resume_meaningful_matches_diagnosis_decided_handoff(fake_workspace):
    """``awaiting_phase_handoff`` WITH a recorded decision: diagnosis stays
    ``needs_decision`` but ``decision_artifact_exists`` flips the unified
    mapping to ``resume_meaningful=True`` (plain resume now applies the
    recorded decision), and the card's next_action points at resume."""
    write_run(
        fake_workspace, "run_decided_handoff",
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x", task="t",
            phase_handoff={
                "id": "h-11",
                "phase": "validate_plan",
                "available_actions": ["continue", "retry_feedback", "halt"],
            },
        ),
        events=[event(1, "phase.start", phase="validate_plan")],
    )
    phase_handoff_decide("run_decided_handoff", "h-11", "continue", cwd=None)

    diag = project_run_diagnosis("run_decided_handoff")
    assert diag.condition == "needs_decision"
    assert diag.decision_artifact_exists is True
    assert _resume_meaningful_from_diagnosis(diag) is True

    card = orcho_run_live_status("run_decided_handoff")
    assert "orcho_run_resume" in card.next_action


def test_resume_meaningful_matches_diagnosis_residual_and_terminal(fake_workspace):
    """The unified mapping agrees with diagnosis across the resumable /
    dead-end split: a residual halted run is ``resume_meaningful=True``; a
    clean terminal ``done`` is ``resume_inert_terminal`` → False; a rejected
    final-acceptance dead-end is ``resume_inert_terminal`` → False with
    ``available_actions=[]`` (diagnose still a terminal, never a decision)."""
    write_run(
        fake_workspace, "run_residual",
        meta=meta(
            status="halted", project="/p/x", task="t",
            halt_reason="pre_run_dirty_halt",
        ),
    )
    write_run(
        fake_workspace, "run_done",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    write_run(
        fake_workspace, "run_rejected",
        meta=meta(
            status="halted", project="/p/x", task="t",
            halt_reason="final_acceptance_rejected",
        ),
    )

    d_residual = project_run_diagnosis("run_residual")
    assert d_residual.condition == "halted"
    assert _resume_meaningful_from_diagnosis(d_residual) is True

    d_done = project_run_diagnosis("run_done")
    assert d_done.condition == "resume_inert_terminal"
    assert _resume_meaningful_from_diagnosis(d_done) is False

    d_rejected = project_run_diagnosis("run_rejected")
    assert d_rejected.condition == "resume_inert_terminal"
    assert d_rejected.available_actions == []
    assert _resume_meaningful_from_diagnosis(d_rejected) is False


def test_resume_meaningful_matches_diagnosis_delivery_gate(fake_workspace):
    """A correction whose fix was already requested is NOT a resumable run:
    diagnosis is ``correction_followup_required`` (the next step is a
    from_run_plan follow-up) and the unified mapping yields
    ``resume_meaningful=False`` — the run is never branded resumable."""
    write_run(
        fake_workspace, "run_correction_gate",
        meta=meta(
            status="halted", project="/p/x", task="t",
            halt_reason="commit_decision_fix",
            commit_delivery={
                "status": "fix_requested",
                "action": "fix",
                "release_verdict": "REJECTED",
                "project_path": "/p/x",
                "source_path": "/p/wt",
                "changed_paths": ["src/a.py"],
                "untracked_paths": [],
            },
        ),
    )

    diag = project_run_diagnosis("run_correction_gate")
    assert diag.condition == "blocked_worktree"
    assert _resume_meaningful_from_diagnosis(diag) is False


# ── bounded payload ──────────────────────────────────────────────────────────

def test_payload_is_bounded(fake_workspace):
    """The card never spills full bodies: a pathological 10 KB event summary
    is truncated to a bounded preview in last_activity."""
    huge = "X" * 10_000
    write_run(
        fake_workspace, "run_bounded",
        meta=meta(status="running", project="/p/x", task="t"),
        events=[
            event(1, "phase.start", phase="implement"),
            event(2, "agent.text", phase="implement", payload={"summary": huge}),
        ],
    )

    card = orcho_run_live_status("run_bounded")

    assert card.last_activity is not None
    preview = card.last_activity.preview
    assert preview is not None
    # Bounded well under the original 10 KB body.
    assert len(preview) <= 256
    assert preview != huge


# ── Correction-followup contract: superseded parent reads closed, not inconsistent ───────────────


def test_live_status_superseded_parent_is_closed(fake_workspace):
    # A rejected-FA parent settled to done + superseded_by_followup (commit
    # delivery evicted) must read as a coherent closed terminal: resume inert,
    # NO done-but-rejected inconsistency, and the next_action names the follow-up.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
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
        ),
    )

    card = orcho_run_live_status("20260101_000001")

    # Coherent closed terminal — never flagged as an active done-but-rejected
    # contradiction.
    assert card.state_class == "terminal_success"
    assert card.consistency_flags == []
    assert card.terminal is not None
    assert card.terminal.resume_meaningful is False
    assert "superseded" in card.next_action
    assert "20260101_000002" in card.next_action


# ── delivery disposition on a terminal_success card ──────────────────────────
#
# A committed / published terminal success carries the cheap delivery
# disposition (committed / published / pr_url) read ONLY on the terminal branch,
# and its next_action points at the PR + evidence delivery slice instead of the
# generic inspect-findings/diff pointer.


def test_terminal_success_delivered_run_carries_disposition_and_pr(fake_workspace):
    """A committed+published terminal success carries the delivery disposition
    and its next_action points at the PR and the evidence delivery slice."""
    write_run(
        fake_workspace, "run_delivered",
        meta=meta(
            status="done", project="/p/x", task="t",
            phases={"final_acceptance": _final_acceptance("APPROVED", True)},
            commit_delivery={
                "status": "committed",
                "action": "approve",
                "release_verdict": "APPROVED",
                "commit_sha": "abc123",
                "pr_url": "https://example.test/pr/42",
                "delivery_notices": ["PR opened: https://example.test/pr/42"],
            },
        ),
        events=[event(1, "run.end", payload={"status": "done"})],
    )

    card = orcho_run_live_status("run_delivered")

    assert card.state_class == "terminal_success"
    term = card.terminal
    assert term is not None
    assert term.delivery_committed is True
    assert term.delivery_published is True
    assert term.delivery_pr_url == "https://example.test/pr/42"
    # next_action names the PR and the read-only evidence delivery slice.
    assert "https://example.test/pr/42" in card.next_action
    assert "slice='delivery'" in card.next_action
    # NOT the generic inspect-findings/diff pointer.
    assert "for findings and orcho_run_diff for" not in card.next_action


def test_terminal_success_committed_no_pr_points_at_delivery_slice(fake_workspace):
    """A committed-but-unpublished terminal success reads
    delivery_committed=True with no pr_url; next_action points at the delivery
    record, not a PR link."""
    write_run(
        fake_workspace, "run_committed_nopr",
        meta=meta(
            status="done", project="/p/x", task="t",
            phases={"final_acceptance": _final_acceptance("APPROVED", True)},
            commit_delivery={
                "status": "committed",
                "action": "approve",
                "release_verdict": "APPROVED",
                "commit_sha": "abc123",
            },
        ),
        events=[event(1, "run.end", payload={"status": "done"})],
    )

    card = orcho_run_live_status("run_committed_nopr")

    term = card.terminal
    assert term is not None
    assert term.delivery_committed is True
    assert term.delivery_published is False
    assert term.delivery_pr_url is None
    assert "slice='delivery'" in card.next_action


def test_running_path_does_not_read_delivery_disposition(
    fake_workspace, monkeypatch,
):
    """The hot running poll must NOT read the delivery disposition — only the
    terminal branch does. The helper is monkeypatched to explode; a running
    card must still build without ever calling it."""
    calls: list[str] = []

    def _boom(run_id: str):
        calls.append(run_id)
        raise AssertionError("delivery_disposition must not run on the hot path")

    monkeypatch.setattr(
        "orcho_mcp.observe.live_status.delivery_disposition", _boom,
    )
    write_run(
        fake_workspace, "run_hot",
        meta=meta(status="running", project="/p/x", task="t"),
        events=[
            event(1, "run.start"),
            event(2, "phase.start", phase="implement"),
        ],
    )

    card = orcho_run_live_status("run_hot")

    assert card.state_class in ("running_phase", "running_subtask")
    assert card.terminal is None
    assert calls == []
