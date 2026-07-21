"""orcho_mcp.run_control.advice — read-only phase-handoff advisory service.

Public ``request_advice`` backs the ``orcho_handoff_advice`` MCP tool. It calls
orcho-core's read-only ``request_handoff_advice`` SDK accessor (a one-shot
advisor pass that writes exactly one durable advice artifact and records NO
decision) and shapes the typed recommendation onto the wire model — including a
deterministic ``ready_next_action`` that carries mandatory provenance for the
EXISTING ``orcho_phase_handoff_decide`` retry verb.

The tool never applies the recommendation: ``ready_next_action`` is a pre-filled
suggestion, not an executed decision. No new runtime verb is introduced — the
retry path reuses ``orcho_phase_handoff_decide(action='retry_feedback', …,
note=<provenance_note>)``.

SDK alias ``_sdk_request_handoff_advice`` lives here so the MCP adapter layer
does not call the SDK directly; the SDK exception types are caught and
translated centrally in ``orcho_mcp.services.errors`` (``RunNotFound`` →
``RunNotFoundError``, ``NoWorkspace`` → ``WorkspaceNotResolvedError``,
``ValueError`` / ``InvalidPhaseHandoffState`` → ``InvalidPlanError``).
"""
from __future__ import annotations

from typing import Any

from sdk import request_handoff_advice as _sdk_request_handoff_advice

from orcho_mcp.schemas import (
    HandoffAdviceResult,
    HandoffAdviceSafetyRecord,
    NextActionRecord,
)
from orcho_mcp.services.errors import map_sdk_errors
from orcho_mcp.services.run_lookup import find_run_dir

#: Advisory verbs whose decision requires a non-empty ``feedback`` string
#: (mirrors the decide path). For ``retry_feedback`` the advisor SUPPLIES that
#: feedback, so the follow-up is a complete ``ready_call``; for
#: ``continue_with_waiver`` the feedback is the operator's waiver verdict — which
#: the advisor cannot fabricate — so the follow-up is ``operator_input_required``.
_FEEDBACK_REQUIRED_ACTIONS = frozenset({"retry_feedback", "continue_with_waiver"})


def _resolve_advisor_provider(run_id: str) -> Any | None:
    """Recover the advisor provider matching how the run was launched.

    The advisor is invoked IN-PROCESS by this read-only accessor (it is not
    spawned through the supervisor like a resume), so unlike the decide path it
    cannot rely on ``--mock`` being re-supplied to a subprocess. A run started
    via ``orcho_run_start(mock=True)`` records ``mock`` in its
    ``mcp_supervisor.json``; without honouring it here the advisor would resolve
    a real (``RealAgentProvider``) in-process LLM call and break the
    mock / no-real-API contract. This seam is what lets the end-to-end
    paused→advice→evidence mock smoke run hermetically (see
    ``tests/acceptance/mock_pipeline/test_handoff_advice_smoke.py``).

    Returns a ``MockAgentProvider`` for a mock run, or ``None`` otherwise — in
    which case the SDK accessor builds the real provider itself, preserving the
    prior behaviour for real runs. Resolution is best-effort: any failure to
    read the supervisor state falls back to ``None`` so the authoritative
    RunNotFound / NoWorkspace error is surfaced by the SDK call (mapped by
    ``map_sdk_errors``), not by provider probing.
    """
    try:
        from agents.runtimes import make_provider

        from orcho_mcp.supervisor.paths import resolve_runs_dir
        from orcho_mcp.supervisor.state import read_state

        run_dir = resolve_runs_dir() / run_id
        state = read_state(run_dir)
        if state is not None and bool(state.get("mock", False)):
            return make_provider(mock=True)
    except Exception:  # noqa: BLE001 — provider probing must never mask SDK errors
        return None
    return None


def request_advice(
    run_id: str,
    handoff_id: str | None = None,
) -> HandoffAdviceResult:
    """Produce a typed advisory recommendation for a paused phase handoff.

    See the ``orcho_handoff_advice`` docstring in ``orcho_mcp.tools`` for the
    wire contract. This module owns the implementation; the tool is a thin shim.

    Error mapping is owned by ``orcho_mcp.services.errors.map_sdk_errors``:
      - ``RunNotFound`` → ``RunNotFoundError``
      - ``NoWorkspace`` → ``WorkspaceNotResolvedError``
      - ``ValueError`` (SDK input validation: empty ``run_id``) →
        ``InvalidPlanError``
      - ``InvalidPhaseHandoffState`` (no active handoff, mismatched
        ``handoff_id``, not paused, or an ineligible handoff) →
        ``InvalidPlanError``

    The SDK accessor is invoked with the provider that matches how the run was
    launched (``MockAgentProvider`` for a ``mock=True`` run — see
    ``_resolve_advisor_provider`` — the real provider otherwise); it performs
    the single durable advice write and never records a decision.
    """
    provider = _resolve_advisor_provider(run_id)
    with map_sdk_errors(run_id):
        run_dir = find_run_dir(run_id)
        advice = _sdk_request_handoff_advice(
            run_id, handoff_id, runs_dir=run_dir.parent, cwd=None, provider=provider,
        )

    return HandoffAdviceResult(
        run_id=advice.run_id,
        handoff_id=advice.handoff_id,
        phase=advice.phase,
        recommended_action=advice.recommended_action,
        confidence=advice.confidence,
        rationale=advice.rationale,
        retry_feedback=advice.retry_feedback,
        risks=list(advice.risks),
        expected_files=list(advice.expected_files),
        operator_note=advice.operator_note,
        parse_warnings=list(advice.parse_warnings),
        safety=HandoffAdviceSafetyRecord(
            auto_apply_ok=advice.safety.auto_apply_ok,
            needs_confirmation=advice.safety.needs_confirmation,
            blocked_reason=advice.safety.blocked_reason,
            waiver_blocked=advice.safety.waiver_blocked,
        ),
        advice_artifact=advice.advice_artifact,
        provenance_note=advice.provenance_note,
        ready_next_action=_build_ready_next_action(advice),
        usage=dict(advice.usage or {}),
    )


def _build_ready_next_action(advice) -> NextActionRecord:  # noqa: ANN001 — SDK dataclass
    """Build the deterministic ``ready_next_action`` for a recommendation.

    Always points at the EXISTING ``orcho_phase_handoff_decide`` verb and always
    carries the provenance ``note`` (so a forwarded decision links back to the
    advice artifact). The tool itself never calls decide — this is a pre-filled
    suggestion only.

    - ``retry_feedback`` → a complete ``ready_call``: ``args`` carry the advisor's
      ``feedback`` and the mandatory ``note=provenance_note``. Safe to forward
      verbatim. (Low confidence is surfaced via ``safety.needs_confirmation``; the
      ready_call is still formed.)
    - ``continue`` / ``halt`` → a ``ready_call`` mirroring that verb (no feedback
      required) — reflects the recommendation without auto-applying it.
    - ``continue_with_waiver`` → ``operator_input_required``: the decision needs
      the operator's waiver verdict as ``feedback``, which the advisor cannot
      supply, so the args intentionally omit it and ``input_schema`` names it.
    """
    action = advice.recommended_action
    base_args = {
        "run_id": advice.run_id,
        "handoff_id": advice.handoff_id,
        "action": action,
        "note": advice.provenance_note,
    }

    if action == "retry_feedback":
        return NextActionRecord(
            intent=(
                "Apply the advisor's recommended retry: re-run the review with "
                "this feedback via the existing phase-handoff decision. The note "
                "carries provenance back to the advice artifact."
            ),
            tool="orcho_phase_handoff_decide",
            args={**base_args, "feedback": advice.retry_feedback},
            optional=True,
            kind="ready_call",
        )

    if action in _FEEDBACK_REQUIRED_ACTIONS:
        # continue_with_waiver: the operator must supply the waiver verdict.
        return NextActionRecord(
            intent=(
                f"Record a {action} decision as the advisor recommends — supply "
                "your operator waiver verdict as 'feedback' first. The advisor "
                "does not fabricate a waiver."
            ),
            tool="orcho_phase_handoff_decide",
            args=base_args,
            optional=True,
            kind="operator_input_required",
            requires_operator_input=True,
            input_schema={
                "feedback": (
                    "The operator waiver verdict: why the rejected findings are "
                    "accepted. Required for continue_with_waiver."
                ),
            },
        )

    # continue / halt (and any non-retry, no-feedback verb): a ready_call that
    # mirrors the recommendation. The tool still does not apply it.
    return NextActionRecord(
        intent=(
            f"Forward the advisor's recommended {action} decision via the "
            "existing phase-handoff decision verb. The tool only recommends — "
            "you choose whether to record it."
        ),
        tool="orcho_phase_handoff_decide",
        args=base_args,
        optional=True,
        kind="ready_call",
    )


__all__ = ["request_advice"]
