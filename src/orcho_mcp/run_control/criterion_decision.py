"""orcho_mcp.run_control.criterion_decision — record a typed human decision.

Backs the ``orcho_criterion_decide`` MCP tool. One responsibility: turn an
operator's explicit ``accept`` / ``reject`` on a ``human`` criterion into the
strict core SDK request, and give a client without native elicitation a
complete, typed way to come back with that verdict.

The single rule the whole module exists to protect: **no decision is ever
inferred.** A verdict reaches core only when a human supplied it — through the
tool argument or through a native MCP form. Chat prose, a reviewer finding, a
transcript line, or an unrelated phase-handoff decision can never satisfy a
criterion, and every path that lacks an explicit verdict returns without
writing anything.

Validation and persistence belong to core. This module resolves *how the
verdict was obtained*; ``services.criterion_projection`` delegates *what it
means*.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field, field_validator

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.schemas.criteria import (
    CriterionDecisionInputRequiredResult,
    CriterionDecisionRecordedResult,
)
from orcho_mcp.schemas.shared import NextActionRecord
from orcho_mcp.services.criterion_projection import (
    project_criterion_matrix,
    record_human_decision,
)

#: The tool a caller replays once it holds the operator's verdict.
DECISION_TOOL = "orcho_criterion_decide"

_ELICITATION_MESSAGE = (
    "Record the operator's verdict on this acceptance criterion. Choose "
    "'accept' only if a human actually exercised it and is satisfied; choose "
    "'reject' otherwise. Do not answer on the operator's behalf."
)

#: The exact missing input, published to clients that cannot elicit natively.
#: Flat and primitive-only, mirroring :class:`_CriterionDecisionInput` — the
#: fallback and the native form must express the same choices, because both
#: feed the identical core SDK request.
CRITERION_DECISION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["accept", "reject"],
            "description": (
                "The operator's verdict on this human criterion. Required — "
                "never inferred from conversation."
            ),
        },
        "note": {
            "type": "string",
            "description": "Optional operator rationale, recorded verbatim.",
        },
    },
    "required": ["decision"],
    "additionalProperties": False,
}


class _CriterionDecisionInput(BaseModel):
    """Native form-elicitation payload for one criterion decision.

    FastMCP elicitation accepts primitive fields only, so ``decision`` stays a
    ``str`` at the transport boundary while publishing the closed enum through
    its JSON Schema and validating the same set below.
    """

    decision: str = Field(
        json_schema_extra={"enum": ["accept", "reject"]},
        description="accept or reject",
    )
    note: str | None = Field(default=None, min_length=1)

    @field_validator("decision")
    @classmethod
    def _closed_set(cls, value: str) -> str:
        if value not in ("accept", "reject"):
            raise ValueError("decision must be 'accept' or 'reject'")
        return value


def _criterion_instructions(run_id: str, criterion_id: str) -> str | None:
    """The criterion's own ``human_instructions``, read through the matrix.

    Best-effort context for the operator, taken from the same single
    projection path every other criterion surface uses. ``None`` when the run
    has no criterion contract or the ID is not a ``manual`` row — the missing
    context never blocks the refusal, which core will reject anyway.
    """
    try:
        matrix = project_criterion_matrix(run_id)
    except Exception:  # noqa: BLE001 - context only; never a new failure mode
        return None
    if matrix is None:
        return None
    for row in matrix.rows:
        if row.criterion_id == criterion_id and row.method.kind == "manual":
            return row.method.instructions
    return None


def _input_required(
    run_id: str, criterion_id: str,
) -> CriterionDecisionInputRequiredResult:
    """The complete no-write refusal for a client that cannot elicit.

    Carries the exact missing-input schema and a ready-call whose ``args``
    already hold every parameter except the operator's verdict, so the caller's
    only remaining job is to ask a human.
    """
    return CriterionDecisionInputRequiredResult(
        run_id=run_id,
        criterion_id=criterion_id,
        reason=(
            "No operator verdict was supplied and this client did not "
            "advertise MCP form elicitation. Nothing was recorded. Ask the "
            "operator to accept or reject this criterion explicitly, then "
            f"call {DECISION_TOOL} again with args.decision. Never infer the "
            "verdict from the conversation."
        ),
        instructions=_criterion_instructions(run_id, criterion_id),
        next_actions=[
            NextActionRecord(
                intent=(
                    "Record the operator's explicit accept / reject verdict "
                    f"for criterion {criterion_id}."
                ),
                tool=DECISION_TOOL,
                args={"run_id": run_id, "criterion_id": criterion_id},
                optional=False,
                kind="operator_input_required",
                requires_operator_input=True,
                choices=["accept", "reject"],
                input_schema=CRITERION_DECISION_INPUT_SCHEMA,
            ),
        ],
    )


def _client_supports_form_elicitation(ctx: Context | None) -> bool:
    """Whether this request's client advertised native form elicitation.

    Delegates to the phase-handoff implementation so the two decision surfaces
    cannot drift on what "capable client" means.
    """
    from orcho_mcp.run_control.handoff import _client_supports_form_elicitation as _c

    return _c(ctx)


async def _elicit_decision(
    ctx: Context, criterion_id: str,
) -> tuple[str, str | None] | None:
    """Ask the client for the verdict natively; ``None`` when it did not answer.

    A cancelled / declined form is not a rejection of the criterion — it is the
    absence of a decision, and the caller must write nothing.
    """
    result = await ctx.elicit(
        message=f"{_ELICITATION_MESSAGE} (criterion {criterion_id})",
        schema=_CriterionDecisionInput,
    )
    if result.action != "accept":
        return None
    note = result.data.note
    return result.data.decision, (note.strip() if note else None)


def decide_criterion(
    run_id: str,
    criterion_id: str,
    decision: Literal["accept", "reject"],
    note: str | None = None,
    actor: str | None = None,
) -> CriterionDecisionRecordedResult:
    """Record an explicit verdict, then read the resulting matrix back.

    The sync core of the action. ``decision`` is already concrete here — the
    elicitation / fallback branch is resolved by
    :func:`decide_criterion_with_elicitation` — so this function only
    delegates and re-projects.

    **The write is the commitment; the readback is a courtesy.** Once
    ``record_human_decision`` returns, the durable journal has changed and
    that fact is irreversible from here: the append-only log will refuse a
    repeat of the same verdict as a duplicate. So a failing readback must
    never be raised as if the decision had failed — an operator seeing an
    error and retrying would get "already decided" and be left unable to tell
    a lost write from a recorded one. Instead the outcome stays
    ``decision_recorded``, ``matrix`` is absent, and ``matrix_error`` names
    the readback failure so the caller re-reads rather than re-decides.

    The guard is deliberately broad: after the write, *nothing* may escape.
    Before the write, every error still surfaces normally.
    """
    record = record_human_decision(
        run_id, criterion_id, decision, note=note, actor=actor,
    )
    try:
        matrix, matrix_error = project_criterion_matrix(run_id), None
    except Exception as exc:  # noqa: BLE001 - never mask a completed write
        # The type name keeps the string non-empty even for an exception with
        # a blank message, which the wire model requires.
        matrix, matrix_error = None, f"{type(exc).__name__}: {exc}"
    return CriterionDecisionRecordedResult(
        run_id=run_id,
        criterion_id=criterion_id,
        decision=record,
        matrix=matrix,
        matrix_error=matrix_error,
    )


async def decide_criterion_with_elicitation(
    run_id: str,
    criterion_id: str,
    decision: Literal["accept", "reject"] | None = None,
    note: str | None = None,
    actor: str | None = None,
    ctx: Context | None = None,
) -> CriterionDecisionRecordedResult | CriterionDecisionInputRequiredResult:
    """Resolve the operator verdict, then record it.

    Three paths, one core SDK request:

    * ``decision`` supplied — delegate straight through.
    * ``decision`` omitted, client advertises form elicitation — request the
      flat ``{decision, note?}`` form natively and build the strict request
      from the answer. A cancelled or declined form writes **nothing** and
      returns the same typed ``operator_input_required`` record as the
      incapable client, so the two clients express the same choices.
    * ``decision`` omitted, no elicitation capability — return
      ``operator_input_required`` with the missing-input schema and a complete
      ready-call. Nothing is written.

    Note precedence: an elicited note is used only when the caller did not
    already supply one, so an explicit ``note`` argument is never overwritten.
    """
    if decision is None:
        if not _client_supports_form_elicitation(ctx):
            return _input_required(run_id, criterion_id)
        assert ctx is not None
        elicited = await _elicit_decision(ctx, criterion_id)
        if elicited is None:
            return _input_required(run_id, criterion_id)
        decision, elicited_note = elicited  # type: ignore[assignment]
        if note is None:
            note = elicited_note

    if decision not in ("accept", "reject"):
        raise InvalidPlanError(
            f"{DECISION_TOOL}: decision must be 'accept' or 'reject', got "
            f"{decision!r}.",
        )

    return decide_criterion(
        run_id, criterion_id, decision, note=note, actor=actor,
    )


__all__ = [
    "CRITERION_DECISION_INPUT_SCHEMA",
    "DECISION_TOOL",
    "decide_criterion",
    "decide_criterion_with_elicitation",
]
