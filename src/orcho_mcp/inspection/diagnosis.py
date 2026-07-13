"""orcho_mcp.inspection.diagnosis — typed run-diagnosis packaging.

Sync public function ``inspect_run_diagnosis`` backs the
``orcho_run_diagnose`` MCP tool. It calls the shared classifier
``services.run_projection.project_run_diagnosis`` (the single source of
truth reused by the resume pre-flight guard) and packs the verdict into a
:class:`orcho_mcp.schemas.RunDiagnosis` with unambiguously typed
``next_actions``.

Call-readiness invariant for every emitted ``NextActionRecord`` (carried in
the typed ``kind`` field, never inferred from ``intent`` prose):

- ``ready_call`` ⇒ ``args`` already hold every required parameter of the
  target tool's actual signature, so the record is safe to forward verbatim.
- ``operator_input_required`` ⇒ a final decision argument is intentionally
  omitted; ``choices`` and/or ``input_schema`` describe the operator input
  still needed.

No public-tool argument ever uses ``parent_run_id`` as a parameter name —
when a parent is the resume target it rides as the ``run_id`` arg of
``orcho_run_resume``.
"""
from __future__ import annotations

from orcho_mcp.schemas import NextActionRecord, RecoveryLineage, RunDiagnosis
from orcho_mcp.services.run_projection import (
    ProviderPressureProjection,
    RunDiagnosisProjection,
    build_provider_pressure,
    project_provider_pressure,
    project_run_diagnosis,
)

# Phase-handoff decision verbs that need no extra operator input: a diagnose
# next-action for these is directly callable (``ready_call``) because
# ``{run_id, handoff_id, action}`` is already the full required-arg set.
_FEEDBACK_FREE_VERBS = frozenset({"continue", "halt"})
# Verbs that REQUIRE operator feedback before the decision is valid; they
# surface as ``operator_input_required`` with an ``input_schema`` for feedback.
_FEEDBACK_REQUIRED_VERBS = frozenset({"retry_feedback", "continue_with_waiver"})

_FEEDBACK_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "feedback": {
            "type": "string",
            "description": (
                "Operator feedback text — required before this decision verb "
                "is valid. Passed as the ``feedback`` arg of "
                "orcho_phase_handoff_decide."
            ),
        },
    },
    "required": ["feedback"],
}

# The closed set of conditions the wire schema enumerates. A residual status
# outside this set is coerced to ``interrupted`` (generic stalled-but-resumable
# run) so the typed wire Literal stays valid.
_KNOWN_CONDITIONS = frozenset({
    "active",
    "needs_decision",
    "needs_delivery_decision",
    "correction_followup_required",
    "closed_by_followup",
    "recover_via_source_run",
    "resume_inert_terminal",
    "superseded_by_child",
    "blocked_worktree",
    "provider_pressure",
    "halted",
    "failed",
    "interrupted",
})

_RESUMABLE_CONDITIONS = frozenset({"halted", "failed", "interrupted"})


def _status_action(run_id: str, *, optional: bool = True) -> NextActionRecord:
    """Ready-to-forward ``orcho_run_status`` inspection call."""
    return NextActionRecord(
        intent="Inspect the run's current status snapshot.",
        tool="orcho_run_status",
        args={"run_id": run_id},
        optional=optional,
        kind="ready_call",
    )


def _evidence_errors_action(run_id: str) -> NextActionRecord:
    """Ready-to-forward ``orcho_run_evidence`` errors-slice inspection call."""
    return NextActionRecord(
        intent="Review the run's errors and halt reason.",
        tool="orcho_run_evidence",
        args={"run_id": run_id, "slice": "errors"},
        optional=True,
        kind="ready_call",
    )


def _watch_action(run_id: str) -> NextActionRecord:
    """Ready-to-forward ``orcho_run_watch`` follow call for a live run."""
    return NextActionRecord(
        intent="Watch the active run until its next handoff or terminal state.",
        tool="orcho_run_watch",
        args={"run_id": run_id},
        optional=True,
        kind="ready_call",
    )


def _resume_action(
    run_id: str, *, intent: str, optional: bool = False,
) -> NextActionRecord:
    """Ready-to-forward ``orcho_run_resume`` call for a resumable target.

    ``run_id`` is the resume target — this run, a recommended follow-up
    child, or a known parent. It always rides as the ``run_id`` arg, never a
    bespoke ``parent_run_id`` parameter.
    """
    return NextActionRecord(
        intent=intent,
        tool="orcho_run_resume",
        args={"run_id": run_id},
        optional=optional,
        kind="ready_call",
    )


def _needs_decision_actions(
    run_id: str, handoff_id: str | None, available_actions: list[str],
) -> list[NextActionRecord]:
    """Typed decide follow-ups for a paused, decision-pending run.

    Per available verb: ``continue`` / ``halt`` become ``ready_call`` records
    (no extra input needed, all required args present), while
    ``retry_feedback`` / ``continue_with_waiver`` become
    ``operator_input_required`` records carrying ``choices`` and the feedback
    ``input_schema``. No ``orcho_phase_handoff_decide`` record is ever a
    ``ready_call`` without a validated, feedback-free action substituted.

    Defensive: when ``handoff_id`` is missing we cannot build a forwardable
    decide call, so we fall back to a single ``operator_input_required``
    record listing the verbs in ``choices`` (no per-verb action).
    """
    if not handoff_id:
        return [
            NextActionRecord(
                intent=(
                    "Resolve the paused phase handoff before resuming "
                    "(choose an action; supply feedback where required)."
                ),
                tool="orcho_phase_handoff_decide",
                args={"run_id": run_id},
                optional=False,
                kind="operator_input_required",
                requires_operator_input=True,
                choices=list(available_actions),
                input_schema=_FEEDBACK_INPUT_SCHEMA,
            ),
        ]

    out: list[NextActionRecord] = []
    for verb in available_actions:
        base_args = {"run_id": run_id, "handoff_id": handoff_id, "action": verb}
        if verb in _FEEDBACK_FREE_VERBS:
            out.append(
                NextActionRecord(
                    intent=f"Resolve the paused handoff with '{verb}'.",
                    tool="orcho_phase_handoff_decide",
                    args=base_args,
                    optional=True,
                    kind="ready_call",
                ),
            )
        elif verb in _FEEDBACK_REQUIRED_VERBS:
            out.append(
                NextActionRecord(
                    intent=(
                        f"Resolve the paused handoff with '{verb}' — supply "
                        "the required feedback."
                    ),
                    tool="orcho_phase_handoff_decide",
                    args=base_args,
                    optional=True,
                    kind="operator_input_required",
                    requires_operator_input=True,
                    choices=[verb],
                    input_schema=_FEEDBACK_INPUT_SCHEMA,
                ),
            )
        else:
            # Unknown verb — surface as operator-input-required so it is never
            # asserted directly callable as a ready_call.
            out.append(
                NextActionRecord(
                    intent=f"Resolve the paused handoff with '{verb}'.",
                    tool="orcho_phase_handoff_decide",
                    args=base_args,
                    optional=True,
                    kind="operator_input_required",
                    requires_operator_input=True,
                    choices=[verb],
                ),
            )
    return out


def _delivery_gate_actions(
    run_id: str, gate_kind: str | None, available_actions: list[str],
) -> list[NextActionRecord]:
    """Ready inspection pointer for an Orcho-managed delivery gate.

    ``orcho_run_diagnose`` stays read-only and points callers to the richer
    gate projection. ``orcho_delivery_gate`` then carries one ready
    ``orcho_delivery_decide`` call per SDK-available action.

    A ``delivery_completed`` gate is terminal — the delivery already landed, so
    there is nothing to decide. Point at the gate projection for the delivered
    outcome (its ``pr_url`` / evidence), never at a delivery decision.
    """
    if gate_kind == "delivery_completed":
        return [
            NextActionRecord(
                intent=(
                    "This Orcho-managed delivery already landed — inspect the "
                    "delivered outcome (pr_url / delivery notices) on the gate "
                    "projection; there is no delivery decision to make."
                ),
                tool="orcho_delivery_gate",
                args={"run_id": run_id},
                optional=False,
                kind="ready_call",
            ),
        ]
    label = "correction" if gate_kind == "correction_decision_required" else "delivery"
    choices = f" Available actions now: {', '.join(available_actions)}." if (
        available_actions
    ) else ""
    return [
        NextActionRecord(
            intent=(
                f"Inspect this {label} gate and choose one of the ready "
                f"orcho_delivery_decide calls it returns.{choices}"
            ),
            tool="orcho_delivery_gate",
            args={"run_id": run_id},
            optional=False,
            kind="ready_call",
        ),
    ]


def _recover_via_source_actions(
    run_id: str, source_run_id: str | None,
) -> list[NextActionRecord]:
    """Typed actions for a terminal recovery run with a resumable source.

    The single deterministic step is ``orcho_run_resume(run_id=<source>)`` —
    the inspected terminal run is explicitly NOT the continuation subject, so
    the resume target is the source checkpoint. Read-only inspection of the
    inert run rides alongside. When the source is somehow unknown the response
    degrades to inspection only (never a resume of the terminal run).
    """
    if not source_run_id:
        return [_status_action(run_id), _evidence_errors_action(run_id)]
    return [
        _resume_action(
            source_run_id,
            intent=(
                f"Resume the source run {source_run_id} that still owns the "
                "retained checkpoint / worktree. The inspected terminal run "
                f"{run_id} is NOT the continuation subject — do not start a "
                "new from_run_plan run to finish its diff."
            ),
            optional=False,
        ),
        _status_action(run_id),
    ]


def _plan_artifact_continuation_actions(run_id: str) -> list[NextActionRecord]:
    """Typed actions for a plan-only run whose subject is a plan artifact.

    ``from_run_plan`` here means "implement THIS persisted plan artifact as a
    NEW run", not "finish the last diff" — the intent says so explicitly. The
    record is a ``ready_call``: ``from_run_plan`` (the source run id) and
    ``profile`` are both present.
    """
    return [
        NextActionRecord(
            intent=(
                f"Start a NEW implementation run from run {run_id}'s persisted "
                "plan artifact. from_run_plan means 'implement this plan from "
                "scratch', NOT 'finish a retained diff or checkpoint'."
            ),
            tool="orcho_run_start",
            args={"from_run_plan": run_id, "profile": "feature"},
            optional=False,
            kind="ready_call",
        ),
        _status_action(run_id),
    ]


def _correction_followup_actions(
    run_id: str,
    project_dir: str | None,
    diff_path: str | None,
    retained_worktree: str | None,
) -> list[NextActionRecord]:
    """Typed actions for a correction whose fix was requested.

    The single deterministic step is an ``orcho_run_start`` from_run_plan
    follow-up carrying the parent's plan + retained diff. Built via the shared
    ``services.delivery_gate.build_followup_next_action`` so the diagnose and the
    delivery-gate surfaces emit a byte-identical typed action — including its
    machine-readable ``context`` (``from_run_plan`` / ``diff_path`` /
    ``project_dir`` / ``retained_worktree``). Read-only status inspection rides
    alongside.
    """
    from orcho_mcp.services.delivery_gate import build_followup_next_action

    return [
        build_followup_next_action(
            run_id, project_dir, diff_path, retained_worktree,
        ),
        _status_action(run_id),
    ]


def _closed_by_followup_actions(
    run_id: str, child_run_id: str | None,
) -> list[NextActionRecord]:
    """Read-only actions for a parent CLOSED by a follow-up.

    The parent is settled/superseded: NO resume of this parent and NO fresh
    ``from_run_plan`` against it. The superseding child (when known) is the live
    subject — point inspection at it; otherwise fall back to the parent's own
    status snapshot. Every record stays read-only.
    """
    if child_run_id:
        return [
            NextActionRecord(
                intent=(
                    f"This run was closed by a successful from_run_plan "
                    f"follow-up ({child_run_id}); inspect the superseding child "
                    "— do NOT resume this parent."
                ),
                tool="orcho_run_status",
                args={"run_id": child_run_id},
                optional=False,
                kind="ready_call",
            ),
            _status_action(run_id),
        ]
    return [_status_action(run_id, optional=False), _evidence_errors_action(run_id)]


def _stop_unknown_actions(
    run_id: str, missing_facts: list[str],
) -> list[NextActionRecord]:
    """Read-only actions for a terminal dead-end with no continuation subject.

    NO ``from_run_plan`` is offered (the durable subject is unknown); the
    intent enumerates the exact missing durable facts so the captain sees why
    no safe continuation could be resolved, and only read-only inspection
    follows.
    """
    missing = ", ".join(missing_facts) if missing_facts else "none recorded"
    return [
        NextActionRecord(
            intent=(
                "No safe continuation subject is known for this terminal run — "
                f"missing durable facts: {missing}. Inspect its outcome; do "
                "NOT start a from_run_plan run as a generic fallback."
            ),
            tool="orcho_run_status",
            args={"run_id": run_id},
            optional=False,
            kind="ready_call",
        ),
        _evidence_errors_action(run_id),
    ]


def _resolve_next_actions(
    proj: RunDiagnosisProjection,
) -> tuple[str, list[NextActionRecord]]:
    """Map a diagnosis projection to (wire condition, typed next_actions)."""
    run_id = proj.run_id
    cond = proj.condition

    if cond == "needs_decision":
        # Once a decision artifact is recorded for the active handoff, the run
        # no longer needs another decide — it needs resume to apply the
        # recorded decision and continue. Route to the same resume the
        # live_status / summary projections already point at (single source of
        # truth: ``RunDiagnosisProjection.decision_artifact_exists``), never a
        # second round of decide verbs.
        if proj.decision_artifact_exists:
            return cond, [
                _resume_action(
                    run_id,
                    intent=(
                        "A phase-handoff decision is already recorded — "
                        "resume to apply it and continue the run."
                    ),
                    optional=False,
                ),
                _status_action(run_id),
            ]
        return cond, _needs_decision_actions(
            run_id, proj.handoff_id, list(proj.available_actions),
        )

    if cond == "needs_delivery_decision":
        return cond, _delivery_gate_actions(
            run_id, proj.delivery_gate_kind, list(proj.available_actions),
        )

    if cond == "correction_followup_required":
        return cond, _correction_followup_actions(
            run_id,
            proj.followup_project_dir,
            proj.followup_diff_path,
            proj.followup_retained_worktree,
        )

    if cond == "closed_by_followup":
        # Correction-followup contract: a parent CLOSED by a successful from_run_plan follow-up.
        # Inspect the superseding child (never resume this settled parent, never
        # offer a fresh from_run_plan against it). Read-only diagnostics only.
        return cond, _closed_by_followup_actions(run_id, proj.recommended_run_id)

    if cond == "superseded_by_child":
        child = proj.recommended_run_id
        if child:
            return cond, [
                _resume_action(
                    child,
                    intent=(
                        f"Resume the active follow-up child {child} that is "
                        "continuing this run's change session."
                    ),
                    optional=False,
                ),
            ]
        return cond, [_status_action(run_id)]

    if cond == "recover_via_source_run":
        return cond, _recover_via_source_actions(run_id, proj.recommended_run_id)

    if cond == "resume_inert_terminal":
        # Terminal run — never a resume of THIS run. The lineage subject still
        # distinguishes a plan-artifact continuation (from_run_plan as a fresh
        # implementation) and a stop/unknown dead-end (read-only, no
        # from_run_plan) from a plain inspection-only terminal.
        if proj.recommended_next_action == "plan_artifact_continuation":
            return cond, _plan_artifact_continuation_actions(run_id)
        if proj.recommended_next_action == "stop_unknown":
            return cond, _stop_unknown_actions(run_id, list(proj.missing_facts))
        # Clean terminal-success / no recovery subject — inspection only.
        return cond, [_evidence_errors_action(run_id), _status_action(run_id)]

    if cond == "blocked_worktree":
        parent = proj.parent_run_id
        if parent:
            return cond, [
                _resume_action(
                    parent,
                    intent=(
                        f"Resume the parent run {parent} to recover its "
                        "undelivered diff this blocked follow-up cannot replay."
                    ),
                    optional=False,
                ),
            ]
        # Unknown parent — read-only diagnostics only, explicitly not a resume.
        return cond, [_status_action(run_id), _evidence_errors_action(run_id)]

    if cond == "active":
        return cond, [_watch_action(run_id), _status_action(run_id)]

    if cond in _RESUMABLE_CONDITIONS:
        return cond, [
            _resume_action(
                run_id,
                intent="Resume this stopped run from its checkpoint.",
                optional=False,
            ),
            _evidence_errors_action(run_id),
        ]

    # Residual status the wire Literal does not enumerate (defensive). Treat as
    # a generic stalled-but-resumable run and label it ``interrupted`` so the
    # typed condition stays valid.
    return "interrupted", [
        _resume_action(
            run_id,
            intent="Resume this stopped run from its checkpoint.",
            optional=False,
        ),
        _evidence_errors_action(run_id),
    ]


def _provider_pressure_reason(pp: ProviderPressureProjection) -> str:
    """Assemble a factual provider-pressure reason — no log prose.

    Built purely from the core-typed projection facts (failure_kind / phase /
    retry_state / recoverable), never from parsed output.
    """
    parts = [f"provider pressure ({pp.failure_kind or 'provider failure'})"]
    if pp.phase:
        parts.append(f"phase {pp.phase}")
    if pp.retry_state:
        parts.append(f"retry_state {pp.retry_state}")
    parts.append("recoverable" if pp.recoverable else "not recoverable")
    return "; ".join(parts)


def inspect_run_diagnosis(run_id: str) -> RunDiagnosis:
    """Diagnose a run's resume situation and return typed next steps.

    See the ``orcho_run_diagnose`` docstring in ``orcho_mcp.tools`` for the
    wire contract. This module owns the implementation; the tool is a thin
    shim. ``RunNotFoundError`` propagates from ``project_run_diagnosis`` for
    an unknown run.

    Post-core provider-pressure reconciliation: when core leaves the run in a
    residual resumable condition (``halted`` / ``failed`` / ``interrupted``)
    but its errors/halt rollup carries a core-typed provider runtime/access
    failure, the condition is upgraded to ``provider_pressure``. The typed
    ``provider_pressure`` payload and its ``next_actions`` come from the single
    shared ``build_provider_pressure`` factory (which calls
    ``build_provider_pressure_next_actions``) — no logic is duplicated here, so
    diagnose can never drift from status / evidence / summary. Non-residual
    conditions (``needs_decision`` / ``needs_delivery_decision`` /
    ``superseded_by_child`` / ``closed_by_followup`` / ``blocked_worktree`` /
    ``resume_inert_terminal`` / ``active`` / …) are never overridden.
    """
    proj = project_run_diagnosis(run_id)
    condition, next_actions = _resolve_next_actions(proj)
    reason = proj.reason
    provider_pressure = None

    if condition in _RESUMABLE_CONDITIONS:
        pp_proj = project_provider_pressure(run_id)
        if pp_proj.condition_present:
            provider_pressure = build_provider_pressure(pp_proj)
            condition = "provider_pressure"
            next_actions = list(provider_pressure.next_actions)
            reason = _provider_pressure_reason(pp_proj)

    return RunDiagnosis(
        run_id=proj.run_id,
        condition=condition,
        reason=reason,
        status=proj.status,
        recommended_run_id=proj.recommended_run_id,
        available_actions=list(proj.available_actions),
        decision_recorded=proj.decision_artifact_exists,
        next_actions=next_actions,
        continuation_subject=proj.continuation_subject,
        recommended_next_action=proj.recommended_next_action,
        recovery_lineage=_recovery_lineage_wire(proj),
        provider_pressure=provider_pressure,
        control=proj.control,
        control_reason=proj.control_reason,
    )


def _recovery_lineage_wire(
    proj: RunDiagnosisProjection,
) -> RecoveryLineage | None:
    """Map the projection's recovery-lineage facts into the wire submodel.

    ``None`` when no recovery lineage was projected (the branches that carry
    no lineage subject — e.g. ``needs_decision`` / ``active``).
    """
    rec = proj.recovery_lineage
    if rec is None:
        return None
    return RecoveryLineage(
        source_run_id=rec.source_run_id,
        source_status=rec.source_status,
        source_resumable=rec.source_resumable,
        active_child_run_id=rec.active_child_run_id,
        plan_subject_available=rec.plan_subject_available,
        missing_facts=list(rec.missing_facts),
    )


__all__ = ["inspect_run_diagnosis"]
