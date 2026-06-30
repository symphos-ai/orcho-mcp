"""orcho_mcp.run_control.handoff — phase-handoff decision service entry.

Public functions in this module back the
``orcho_phase_handoff_decide`` MCP tool. Pure state transition over
SDK helpers — never spawns a process; the post-decision continuation
goes through ``resume_run`` separately.

SDK alias ``_sdk_phase_handoff_decide`` lives here so the MCP adapter
layer does not call the SDK directly; the SDK exception types are caught
and translated centrally in ``orcho_mcp.services.errors``.
"""
from __future__ import annotations

from mcp import types as mcp_types
from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field
from sdk import phase_handoff_decide as _sdk_phase_handoff_decide

from orcho_mcp.errors import (
    InspectOnlyControlError,
    InvalidPlanError,
    OrchoMCPError,
)
from orcho_mcp.run_control.lifecycle import build_inspect_only_control_result
from orcho_mcp.schemas import PhaseHandoffDecideResult
from orcho_mcp.services.errors import map_sdk_errors
from orcho_mcp.services.run_control_boundary import project_run_control

# Actions that require a non-empty ``feedback`` string. ``retry_feedback``
# injects it as the critique for one extra plan round; ``continue_with_waiver``
# records it as the durable operator waiver. Both reuse the same native
# feedback-elicitation path so a capable client can supply the missing
# feedback without a chat round-trip. This MCP-side set mirrors the SDK's
# own ``_FEEDBACK_REQUIRED_ACTIONS`` (``pipeline.runtime.roles``
# RETRY_FEEDBACK / CONTINUE_WITH_WAIVER) so the boundary and the core never
# disagree on which verbs gate on feedback.
_FEEDBACK_REQUIRED_ACTIONS = frozenset({"retry_feedback", "continue_with_waiver"})
_FEEDBACK_ELICITATION_MESSAGE = (
    "Explain what the reviewer should reconsider — be concrete about "
    "the finding, the change you want, or the missing context."
)


def _missing_required_feedback(action: str, feedback: str | None) -> bool:
    """Whether ``action`` needs feedback but none (or only whitespace) was given.

    Mirrors the SDK's feedback rule exactly: ``retry_feedback`` and
    ``continue_with_waiver`` require a non-empty, non-whitespace string.
    """
    return action in _FEEDBACK_REQUIRED_ACTIONS and (
        feedback is None
        or not isinstance(feedback, str)
        or not feedback.strip()
    )


def _require_feedback_or_raise(action: str, feedback: str | None) -> None:
    """Raise a structured ``InvalidPlanError`` when required feedback is absent.

    The message names the exact field the caller must supply
    (``feedback``) and what each verb does with it, so the error makes the
    missing input obvious *before* the SDK call is attempted — never an
    opaque traceback. This is the MCP-owned boundary mirror of the SDK's
    own feedback validation (same ``_FEEDBACK_REQUIRED_ACTIONS`` set), so
    the two never disagree.
    """
    if _missing_required_feedback(action, feedback):
        raise InvalidPlanError(
            f"orcho_phase_handoff_decide: action {action!r} requires a "
            "non-empty 'feedback' string. retry_feedback injects it as the "
            "next plan round's critique; continue_with_waiver records it as "
            "the durable operator verdict that waives the rejected findings. "
            "Supply args.feedback and retry.",
        )


def _raise_if_inspect_only(run_id: str) -> None:
    """Refuse a decide on a run MCP cannot control by raising, else return.

    Reads the durable controllability classification via
    :func:`project_run_control`. A foreign / CLI-started run
    (``control='inspect_only'`` — no durable ``mcp_supervisor.json``) is
    short-circuited by *raising* :class:`InspectOnlyControlError` (carrying the
    typed :class:`InspectOnlyControlResult` payload) BEFORE any feedback
    validation, native elicitation, SDK call, or decision-artifact write, so no
    decision is ever recorded for a run MCP did not start. Raising — rather than
    returning the refusal in the success union — keeps
    ``orcho_phase_handoff_decide``'s success ``outputSchema`` byte-identical to
    before the guard existed.

    Defensive: an unresolvable / corrupt run yields no classification, so the
    function returns and the existing SDK path keeps its resolution + error
    contract (``RunNotFound`` still raises there), never a new failure surface
    from the pre-flight classifier.
    """
    try:
        projection = project_run_control(run_id)
    except OrchoMCPError:
        return
    if projection.control == "inspect_only":
        raise InspectOnlyControlError(
            build_inspect_only_control_result(
                run_id, "phase_handoff_decide", reason=projection.reason,
            ),
        )


class _FeedbackInput(BaseModel):
    """Native form-elicitation payload for a feedback-gated decision."""

    feedback: str = Field(
        min_length=1,
        description=_FEEDBACK_ELICITATION_MESSAGE,
    )


def decide_phase_handoff(
    run_id: str,
    handoff_id: str,
    action: str,
    feedback: str | None = None,
    note: str | None = None,
) -> PhaseHandoffDecideResult:
    """Resolve a pipeline paused at ``status=awaiting_phase_handoff``.

    See ``orcho_phase_handoff_decide`` docstring in ``orcho_mcp.tools``
    for the wire contract. This module owns the implementation; the
    tool is a thin sync shim.

    Control guard (first step): a run MCP did not start
    (``control='inspect_only'``) is refused by raising
    :class:`InspectOnlyControlError` BEFORE feedback validation and the SDK
    call, so no decision artifact is written for a foreign / CLI-started run.

    Error mapping is owned by ``orcho_mcp.services.errors.map_sdk_errors``:
      - ``RunNotFound`` → ``RunNotFoundError``
      - ``NoWorkspace`` → ``WorkspaceNotResolvedError``
      - ``ValueError`` (SDK input validation: bad ``action``,
        ``retry_feedback`` / ``continue_with_waiver`` without feedback,
        malformed ``run_id`` / ``handoff_id``) → ``InvalidPlanError``
      - ``InvalidPhaseHandoffState`` (state / contract mismatch: wrong
        status, mismatched handoff id, action not in
        ``available_actions``, payload-divergence conflict) →
        ``InvalidPlanError``

    The last two both land on ``InvalidPlanError`` so clients can
    distinguish missing-run from bad-request.

    Feedback-gated actions (``retry_feedback`` / ``continue_with_waiver``)
    are validated at the boundary first: a missing / whitespace-only
    ``feedback`` raises a structured ``InvalidPlanError`` naming the field
    before the SDK call is attempted, so the caller learns exactly what to
    supply without an opaque traceback. (The async
    ``decide_phase_handoff_with_elicitation`` wrapper tries native
    elicitation before reaching here.)
    """
    _raise_if_inspect_only(run_id)

    _require_feedback_or_raise(action, feedback)

    with map_sdk_errors(run_id):
        result = _sdk_phase_handoff_decide(
            run_id,
            handoff_id,
            action,
            feedback=feedback,
            note=note,
            cwd=None,
        )

    return PhaseHandoffDecideResult(
        run_id=result.run_id,
        handoff_id=result.handoff_id,
        phase=result.phase,
        action=result.action,
        feedback=result.feedback,
        note=result.note,
        decided_at=result.decided_at,
        # MCP UX A1: pass through next_actions from the SDK result.
        # For continue / retry_feedback / continue_with_waiver the SDK
        # fills with a single mandatory orcho_run_resume action; for
        # halt it derives follow-ups from the post-halt run state.
        next_actions=[a.to_dict() for a in result.next_actions],
    )


def _client_supports_form_elicitation(ctx: Context | None) -> bool:
    """Return whether this request's client advertised elicitation."""
    if ctx is None:
        return False
    return ctx.session.check_client_capability(
        mcp_types.ClientCapabilities(
            elicitation=mcp_types.ElicitationCapability(
                form=mcp_types.FormElicitationCapability(),
            ),
        ),
    )


async def _elicit_feedback(ctx: Context, action: str) -> str:
    """Ask the client for decision feedback through native MCP elicitation.

    Shared by every feedback-gated action (``retry_feedback`` and
    ``continue_with_waiver``); ``action`` only flavours the error
    message so the agent knows which verb it must supply feedback for.
    """
    result = await ctx.elicit(
        message=_FEEDBACK_ELICITATION_MESSAGE,
        schema=_FeedbackInput,
    )
    if result.action != "accept":
        raise InvalidPlanError(
            f"{action} requires feedback. Native MCP elicitation "
            f"was {result.action} by the client; ask the user for "
            "feedback in chat and retry with args.feedback.",
        )
    feedback = result.data.feedback.strip()
    if not feedback:
        raise InvalidPlanError(f"{action} requires non-empty feedback")
    return feedback


async def decide_phase_handoff_with_elicitation(
    run_id: str,
    handoff_id: str,
    action: str,
    feedback: str | None = None,
    note: str | None = None,
    ctx: Context | None = None,
) -> PhaseHandoffDecideResult:
    """Resolve a handoff, requesting feedback natively when available.

    ``choices`` remain the canonical menu. For the feedback-gated
    actions (``retry_feedback`` and ``continue_with_waiver``) the
    advertised args intentionally omit ``feedback``. If a capable MCP
    client forwards those args as-is, this service requests feedback
    through form elicitation. Clients without elicitation support keep
    the existing fallback: the agent asks in chat and supplies
    ``feedback`` before calling this tool.

    Control guard (first step): a run MCP did not start
    (``control='inspect_only'``) is refused by raising
    :class:`InspectOnlyControlError` BEFORE any native elicitation and before
    the sync delegate's SDK call, so no elicitation round-trip or decision
    artifact happens for a foreign / CLI-started run.
    """
    _raise_if_inspect_only(run_id)

    if (
        action in _FEEDBACK_REQUIRED_ACTIONS
        and feedback is None
        and _client_supports_form_elicitation(ctx)
    ):
        assert ctx is not None
        feedback = await _elicit_feedback(ctx, action)

    return decide_phase_handoff(
        run_id, handoff_id, action, feedback=feedback, note=note,
    )


__all__ = ["decide_phase_handoff", "decide_phase_handoff_with_elicitation"]
