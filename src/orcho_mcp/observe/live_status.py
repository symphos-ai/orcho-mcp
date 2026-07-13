"""orcho_mcp.observe.live_status — bounded live-status card builder.

Backs the ``orcho_run_live_status`` MCP tool. The agent's question while
babysitting a run is "where is it right now, and what should I do?" —
answered in a single bounded, operator-safe payload that unites the
durable meta status (with supervisor terminal fallback), the live
phase/subtask position, the last significant activity, any pending
phase-handoff, and terminal consistency — without raw log scraping.

Everything is **reused, not re-derived**:

- The live position (status, current_phase, current_subtask, last event,
  next_seq, pending-handoff compact fields) comes from
  :func:`build_latest_run_events_summary` — the same bounded event walk
  ``orcho_run_events_summary`` uses, so the two tools never drift.
- The handoff ``default_action`` heuristic + ``findings_summary`` come
  from :func:`build_handoff_hint` (which parses ``meta.phase_handoff``
  only inside ``services.run_projection``, the projection owner).
- The terminal coherence + ``final_acceptance`` verdict come from
  :func:`orcho_mcp.services.run_projection.project_terminal_consistency`.
- ``resume_meaningful`` (and the terminal ``next_action``) come from the
  unified :func:`orcho_mcp.services.run_projection.project_run_diagnosis`
  authority — the same classifier behind ``orcho_run_diagnose`` and the
  resume pre-flight — so the card never advertises a resume those surfaces
  would block (a rejected / inert terminal reads ``resume_meaningful=false``).

This module therefore performs **no direct SDK read** of its own — every
durable read flows through the existing summary / hint / projection
owners — so it stays off the architectural SDK-sentinel surface. All
embedded previews are truncated (via ``observe.summary._truncate``) so
the high-frequency poll never spills raw payload.
"""
from __future__ import annotations

from typing import Literal

from orcho_mcp.observe.handoff_hints import build_handoff_hint
from orcho_mcp.observe.summary import _truncate, build_latest_run_events_summary
from orcho_mcp.schemas import (
    CompactRunEvent,
    CurrentSubtaskRecord,
    PendingHandoffSummary,
    RunLiveActivity,
    RunLiveHandoff,
    RunLiveStatusCard,
    RunLiveTerminal,
)
from orcho_mcp.schemas.observe import HandoffDecisionHint
from orcho_mcp.schemas.shared import ProviderPressure
from orcho_mcp.services.delivery_gate import (
    DeliveryDisposition,
    delivery_disposition,
)
from orcho_mcp.services.run_projection import (
    RunDiagnosisProjection,
    TerminalConsistencyProjection,
    build_provider_pressure,
    project_provider_pressure,
    project_run_diagnosis,
    project_terminal_consistency,
)

# Preview cap for the last-activity text. Matches observe.summary's
# ``_COMPACT_SUMMARY_MAX`` so the live card and the events summary truncate
# the same human-readable extract to the same budget. The event preview is
# already bounded upstream (``CompactRunEvent.summary`` is capped at 256);
# re-applying the cap here keeps the card honest if that ever changes.
_LIVE_ACTIVITY_PREVIEW_MAX = 256

# State-class labels — mirror the closed ``RunLiveStatusCard.state_class``
# Literal so the classifier stays in one vocabulary.
_StateClass = Literal[
    "running_phase",
    "running_subtask",
    "awaiting_handoff",
    "terminal_success",
    "terminal_halted",
    "terminal_inconsistent",
]

# Terminal failure statuses that read as a halted terminal card. Mirrors
# the projection owner's vocabulary; ``halted`` is also caught by the
# projection's ``is_halted`` flag.
_TERMINAL_FAILURE_STATUSES = frozenset({
    "failed", "interrupted", "halted", "orphaned",
})

_TERMINAL_CLASSES = frozenset({
    "terminal_success", "terminal_halted", "terminal_inconsistent",
})

# The empty terminal delivery disposition — the default for every non-terminal
# card (kept as a module-level singleton so it is never re-constructed in an
# argument default; the disposition is only truly read on the terminal branch).
_EMPTY_DISPOSITION = DeliveryDisposition()

# Diagnosis conditions under which a plain ``orcho_run_resume`` of THIS run is
# inert or needs a prior operator action — resume is therefore NOT meaningful.
# Mirrors the condition vocabulary owned by
# ``services.run_projection.project_run_diagnosis`` (kept as literals so this
# presentation module does not reach into the projection's private condition
# constants). Any OTHER condition is a residual-resumable status — the diagnosis
# residual branch surfaces the run's own status as the condition — for which a
# plain resume advances the run.
_NON_RESUMABLE_CONDITIONS = frozenset({
    "needs_decision",            # resolved via decision_artifact_exists below
    "needs_delivery_decision",
    "correction_followup_required",  # next step is a from_run_plan follow-up
    "closed_by_followup",            # parent closed by a successful follow-up
    "superseded_by_child",
    "blocked_worktree",
    "recover_via_source_run",
    "resume_inert_terminal",
    "active",
})


def _resume_meaningful_from_diagnosis(diag: RunDiagnosisProjection) -> bool:
    """Whether a plain ``orcho_run_resume`` of this run advances it right now.

    The single unified semantics shared with ``orcho_run_diagnose`` and the
    resume pre-flight guard (all three consume ``project_run_diagnosis``):
    ``True`` only for (a) a residual-resumable status (non-terminal halted /
    failed / interrupted — the diagnosis residual branch) and (b) an
    ``awaiting_phase_handoff`` pause whose decision artifact is already
    recorded (``decision_artifact_exists`` → resume applies it). ``False``
    for every dead-end (terminal success, ``resume_inert_terminal``,
    ``recover_via_source_run``, ``superseded_by_child``), every pending
    decision surface (unresolved ``needs_decision`` / ``needs_delivery_decision``
    / ``blocked_worktree``) and a live ``active`` run — so no surface
    advertises a resume the pre-flight would block.
    """
    if diag.condition == "needs_decision":
        return diag.decision_artifact_exists
    return diag.condition not in _NON_RESUMABLE_CONDITIONS


def _classify_state(
    status: str | None,
    current_subtask: CurrentSubtaskRecord | None,
    tc: TerminalConsistencyProjection,
    pending: PendingHandoffSummary | None,
) -> _StateClass:
    """Classify the run's live state into one closed ``state_class``.

    Priority is deterministic: a pending phase-handoff is reported as a
    decision even when stale terminal fields sit underneath; a terminal
    success whose final_acceptance contradicts it surfaces as
    ``terminal_inconsistent`` (never hidden); otherwise a halted / failed
    terminal, then a running subtask, then a running phase. A non-running,
    non-terminal pause the closed Literal cannot name (e.g.
    ``awaiting_gate_decision``) falls through conservatively to
    ``running_phase`` ("still in progress, keep polling").
    """
    if pending is not None:
        return "awaiting_handoff"
    if tc.is_terminal_success:
        return "terminal_inconsistent" if tc.inconsistencies else "terminal_success"
    if tc.is_halted or status in _TERMINAL_FAILURE_STATUSES:
        return "terminal_halted"
    if current_subtask is not None:
        return "running_subtask"
    return "running_phase"


def _build_last_activity(
    last_n: list[CompactRunEvent],
) -> RunLiveActivity | None:
    """Project the most recent event into a compact activity coordinate.

    Reuses the already-bounded ``CompactRunEvent`` projection from the
    events summary; the preview is the first non-empty of its
    summary / status / tool, re-truncated defensively.
    """
    if not last_n:
        return None
    evt = last_n[-1]
    preview = evt.summary or evt.status or evt.tool
    return RunLiveActivity(
        kind=evt.kind,
        ts=evt.ts,
        phase=evt.phase,
        preview=_truncate(preview, _LIVE_ACTIVITY_PREVIEW_MAX),
    )


def _build_live_handoff(
    pending: PendingHandoffSummary,
    hint: HandoffDecisionHint | None,
) -> RunLiveHandoff:
    """Compose the compact handoff slice from the existing projections.

    Operator fields (handoff_id / phase / verdict / available_actions /
    recommended action) come from the pending-handoff projection; the
    ``default_action`` heuristic and ``findings_summary`` come from the
    handoff hint (``None``-safe when the hint could not be built).
    """
    default_action = hint.default_action if hint else None
    findings_summary = hint.findings_summary if hint else None
    return RunLiveHandoff(
        handoff_id=pending.handoff_id,
        phase=pending.phase,
        available_actions=list(pending.available_actions),
        default_action=default_action,
        verdict=pending.verdict,
        findings_summary=findings_summary,
        recommended_action=pending.suggested_next_action,
    )


def _build_live_terminal(
    tc: TerminalConsistencyProjection,
    resume_meaningful: bool,
    disposition: DeliveryDisposition,
) -> RunLiveTerminal:
    """Compose the terminal slice from the terminal-consistency projection.

    ``resume_meaningful`` is supplied by the caller from the unified
    ``project_run_diagnosis`` authority (NOT ``tc.resume_meaningful``, which
    only knew ``not is_terminal_success`` and so wrongly read a rejected
    terminal halt as resumable). ``disposition`` is the cheap terminal delivery
    read (``services.delivery_gate.delivery_disposition``), computed by the
    caller only on the terminal branch. Every other terminal field is the narrow
    coherence read the consistency projection owns.
    """
    return RunLiveTerminal(
        halt_reason=tc.halt_reason,
        final_acceptance=tc.final_acceptance_verdict,
        final_acceptance_rejected=tc.final_acceptance_rejected,
        resume_meaningful=resume_meaningful,
        inconsistencies=list(tc.inconsistencies),
        delivery_committed=disposition.committed,
        delivery_published=disposition.published,
        delivery_pr_url=disposition.pr_url,
    )


def _delivered_next_action(disposition: DeliveryDisposition) -> str:
    """Next-step pointer for a terminal success whose delivery already landed.

    Points at the pull request (its live ``pr_url``) when the delivery was
    published, else at the delivered checkout, and always at the read-only
    ``orcho_run_evidence`` delivery slice for the delivery record — never at a
    stale 'inspect diff / commit directly' hint.
    """
    if disposition.published and disposition.pr_url:
        return (
            "delivery committed and a pull request is open "
            f"({disposition.pr_url}) — review or merge the PR; inspect "
            "orcho_run_evidence (slice='delivery') for the delivery record"
        )
    return (
        "delivery committed to the target checkout — inspect orcho_run_evidence "
        "(slice='delivery') for the delivery record and orcho_run_diff for the "
        "delivered changes"
    )


def _live_next_action(
    state_class: _StateClass,
    pending: PendingHandoffSummary | None,
    resume_meaningful: bool,
    superseded_child: str | None = None,
    disposition: DeliveryDisposition = _EMPTY_DISPOSITION,
) -> str:
    """Conservative one-line next-step pointer derived from ``state_class``.

    No invention: a paused run reuses the projection's suggested action; a
    terminal card points at the right inspect / resume tool; a live run
    points back at the poll loop. ``resume_meaningful`` is the unified
    diagnosis verdict, so a terminal halt only points at ``orcho_run_resume``
    when the pre-flight would actually let it spawn — a rejected / inert
    terminal points at evidence only. ``disposition`` (the cheap terminal
    delivery read) redirects a delivered ``terminal_success`` at its PR /
    delivery record instead of a generic inspect-diff hint.
    """
    if state_class == "awaiting_handoff":
        if pending is not None and pending.suggested_next_action:
            return pending.suggested_next_action
        return (
            "decide via orcho_phase_handoff_decide, then orcho_run_resume "
            "to continue"
        )
    if state_class == "terminal_inconsistent":
        return (
            "run reports terminal success but final_acceptance is REJECTED — "
            "inspect orcho_run_evidence and do not treat the run as shipped"
        )
    if state_class == "terminal_halted":
        if resume_meaningful:
            return (
                "inspect orcho_run_evidence for the halt cause, then "
                "orcho_run_resume to continue if appropriate"
            )
        return "inspect orcho_run_evidence for the halt cause"
    if state_class == "terminal_success":
        if superseded_child:
            return (
                "this run was superseded by a successful from_run_plan "
                f"follow-up ({superseded_child}); it is closed — inspect the "
                "follow-up child, do not resume this parent"
            )
        if disposition.committed:
            return _delivered_next_action(disposition)
        return (
            "inspect orcho_run_evidence for findings and orcho_run_diff for "
            "changes"
        )
    return "poll orcho_run_live_status (or orcho_run_watch) for further progress"


def _provider_pressure_next_action(pp: ProviderPressure) -> str:
    """Resume-later / inspect pointer for a provider-pressure terminal card.

    Deliberately NOT phrased as an ordinary failure: provider pressure is a
    rate-limit / transient runtime / access fault, not a rejected review, a
    failed final acceptance, or an operator halt. The wording points at the
    evidence slice and a later resume (or a wait until the reset window when
    core supplies one), never at a decision the run does not need.
    """
    if pp.reset_at:
        return (
            "provider under pressure — wait until the reset window "
            f"({pp.reset_at}) passes, then orcho_run_resume; inspect "
            "orcho_run_evidence (slice='errors') for the provider signal"
        )
    return (
        "provider under pressure — inspect orcho_run_evidence "
        "(slice='errors') for the provider signal, then orcho_run_resume "
        "to retry once provider capacity returns"
    )


def build_run_live_status(run_id: str) -> RunLiveStatusCard:
    """Return a bounded operator-safe live status card for a mono run.

    Backs the ``orcho_run_live_status`` MCP tool. Composes the live event
    position (status / current_phase / current_subtask / last activity /
    next_seq / pending-handoff), the handoff ``default_action`` +
    ``findings_summary`` hint, and the terminal-consistency projection
    into one typed card — reusing existing owners, never re-parsing
    ``meta.phase_handoff`` or ``meta.json`` here.

    Raises ``RunNotFoundError`` (via the underlying readers) when
    ``run_id`` does not exist on disk.
    """
    snap = build_latest_run_events_summary(run_id)
    status = snap.status
    pending = snap.pending_handoff

    tc = project_terminal_consistency(run_id)

    # ``build_handoff_hint`` short-circuits to ``None`` for any non-paused
    # status, so the hot running path never pays for the handoff read-model
    # (it only resolves findings/default_action when actually paused).
    hint = build_handoff_hint(run_id, snap)

    state_class = _classify_state(status, snap.current_subtask, tc, pending)

    # ``resume_meaningful`` (and the terminal next_action) come from the single
    # unified ``project_run_diagnosis`` authority — the same classifier behind
    # ``orcho_run_diagnose`` and the resume pre-flight — so the live card never
    # advertises a resume those surfaces would block. Only terminal cards carry
    # ``resume_meaningful``; the running / awaiting paths do not need it, so the
    # heavier diagnosis (lineage + gate reads) stays off the hot poll path and
    # is computed lazily only for a terminal ``state_class``.
    resume_meaningful = False
    provider_pressure: ProviderPressure | None = None
    # The delivery disposition is a terminal-only read: computed lazily here so
    # the hot running / awaiting poll never pays for the commit-delivery meta
    # read. Defaults to the empty disposition for every non-terminal card.
    disposition = _EMPTY_DISPOSITION
    if state_class in _TERMINAL_CLASSES:
        resume_meaningful = _resume_meaningful_from_diagnosis(
            project_run_diagnosis(run_id),
        )
        # Core-typed provider pressure, from the SAME source + shared helper as
        # status / diagnose / evidence / summary. Only meaningful on a terminal
        # card; the running / awaiting paths skip it to keep the hot poll cheap.
        provider_pressure = build_provider_pressure(
            project_provider_pressure(run_id),
        )
        # Cheap single-source delivery disposition (committed / published /
        # pr_url) — reads only meta.commit_delivery via the shared
        # ``services.delivery_gate`` helper, no full gate projection.
        disposition = delivery_disposition(run_id)

    handoff_model = (
        _build_live_handoff(pending, hint)
        if state_class == "awaiting_handoff" and pending is not None
        else None
    )
    terminal_model = (
        _build_live_terminal(tc, resume_meaningful, disposition)
        if state_class in _TERMINAL_CLASSES else None
    )

    # Provider-pressure overrides the generic terminal-halted pointer with a
    # resume-later / inspect phrasing — never a review/delivery/operator-halt
    # rejection — while keeping the typed condition in ``provider_pressure``.
    next_action = (
        _provider_pressure_next_action(provider_pressure)
        if provider_pressure is not None
        else _live_next_action(
            state_class,
            pending,
            resume_meaningful,
            tc.superseded_by_followup,
            disposition,
        )
    )

    return RunLiveStatusCard(
        run_id=run_id,
        status=status,
        state_class=state_class,
        current_phase=snap.current_phase,
        current_subtask=snap.current_subtask,
        last_activity=_build_last_activity(snap.last_n),
        pending_handoff=handoff_model,
        terminal=terminal_model,
        next_action=next_action,
        consistency_flags=list(tc.inconsistencies),
        next_seq=snap.next_seq,
        provider_pressure=provider_pressure,
    )


__all__ = ["build_run_live_status"]
