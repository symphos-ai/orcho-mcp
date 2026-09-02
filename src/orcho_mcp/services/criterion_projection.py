"""orcho_mcp.services.criterion_projection — the ONE criterion projection path.

Every MCP surface that shows criterion facts — the ``plan`` and
``criterion_matrix`` evidence slices, ``orcho_run_status``,
``orcho_run_diagnose``, ``orcho_delivery_gate``, and the criterion-decision
action — reads through this module. There is exactly one SDK call site per
fact, so those surfaces cannot disagree about blockers, readiness, or the
next action.

What this module does **not** do is as load-bearing as what it does. It never
derives a row state, a readiness verdict, an executor set, receipt freshness,
gate selection, or a blocking consequence. Those are orcho-core's
(``pipeline.criterion_matrix``, ADR 0188); here they are validated into wire
models and forwarded. The architecture guard
``tests/unit/architecture/test_criterion_boundary.py`` fails the build if a
second SDK call site or a local derivation appears.

SDK aliases (``_sdk_get_criterion_matrix``, ``_sdk_list_criterion_decisions``,
``_sdk_record_criterion_decision``) live here so the adapter layer does not
call the SDK directly, matching the convention in ``inspection/evidence.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.schemas.criteria import (
    CriterionMatrixRecord,
    CriterionMatrixSummaryRecord,
    HumanCriterionDecisionRecord,
    PlanCriterionRecord,
    TaskAcceptanceRefsRecord,
)
from orcho_mcp.schemas.shared import NextActionRecord
from orcho_mcp.services.errors import map_sdk_errors

# The ADR 0188 SDK slice is newer than the rest of the read surface. Import it
# DEFENSIVELY so a version-skewed orcho-core does not break module import and
# take down unrelated MCP tools. Criterion reads themselves still fail loudly:
# an unavailable capability is not equivalent to an SDK returning ``None`` for
# a run that genuinely has no criterion contract.
try:
    from sdk import (
        get_criterion_matrix as _sdk_get_criterion_matrix,
        list_criterion_decisions as _sdk_list_criterion_decisions,
        record_criterion_decision as _sdk_record_criterion_decision,
    )
except ImportError:  # pragma: no cover - exercised by the stale-core unit test
    _sdk_get_criterion_matrix = None
    _sdk_list_criterion_decisions = None
    _sdk_record_criterion_decision = None

#: Message for a core build without the ADR 0188 SDK slice. Every criterion
#: read and write path raises it rather than presenting version skew as an
#: absent run contract.
STALE_CORE_MESSAGE = (
    "the connected orcho-core does not expose the ADR 0188 criterion SDK "
    "(sdk.get_criterion_matrix / sdk.record_criterion_decision). Point "
    "orcho-mcp at a core build that includes the criterion contract."
)


def criterion_sdk_available() -> bool:
    """Whether the connected orcho-core exposes the criterion SDK slice."""
    return _sdk_get_criterion_matrix is not None


# ── read ─────────────────────────────────────────────────────────────────────


def project_criterion_matrix(run_id: str) -> CriterionMatrixRecord | None:
    """The run's criterion matrix, or ``None`` when the run has none.

    ``None`` means the ``criterion_matrix`` key is genuinely **absent** — a run
    predating the contract, or one with no accepted plan. Callers must omit
    their key rather than emit ``null``. A new-format plan that declares no
    criteria is a *present* matrix with ``rows == []`` and the zeroed summary;
    the two cases stay distinguishable all the way to the wire.
    """
    if _sdk_get_criterion_matrix is None:
        raise InvalidPlanError(f"criterion_matrix unavailable: {STALE_CORE_MESSAGE}")
    with map_sdk_errors(run_id):
        raw = _sdk_get_criterion_matrix(run_id, cwd=None)
        if raw is None:
            return None
        return CriterionMatrixRecord.model_validate(raw)


def project_criterion_readiness(run_id: str) -> CriterionMatrixSummaryRecord | None:
    """The criterion readiness summary for status / diagnose / delivery.

    A projection of the very same matrix read — deliberately not a second,
    cheaper source. Sharing one read is what makes the four surfaces agree on
    ``blocking_open`` / ``ready`` / ``pending_human_ids`` by construction.
    """
    matrix = project_criterion_matrix(run_id)
    return None if matrix is None else matrix.summary


def read_criterion_readiness(run_id: str) -> CriterionMatrixSummaryRecord | None:
    """Read readiness for status / diagnose / delivery without masking errors.

    Only an actual ``None`` returned by the available core SDK means that the
    run has no criterion contract. Version skew, malformed evidence, and an
    invalid current matrix remain explicit failures on every public surface;
    otherwise a client could mistake a degraded proof system for readiness.

    Cost note: the only public core projection of the matrix composes the whole
    evidence bundle, so these enrichment reads are not free. If status polling
    cost becomes a problem the remedy is a narrow core readiness projection,
    NOT an MCP-side cache or a second derivation of the summary.
    """
    return project_criterion_readiness(run_id)


def project_plan_criteria(criteria: Iterable[Any]) -> list[PlanCriterionRecord]:
    """Project the SDK plan summary's typed criteria onto the wire.

    The SDK hands over durable criterion dicts — core's single legacy ingress
    normalizer has already turned any old ``list[str]`` artifact into typed
    criteria upstream, so nothing here classifies, stringifies, or invents an
    ID. A payload that is not a typed criterion is a contract break and fails
    validation loudly rather than degrading to prose.
    """
    return [PlanCriterionRecord.model_validate(c) for c in criteria]


def project_task_acceptance_refs(
    refs: Iterable[Any],
) -> list[TaskAcceptanceRefsRecord]:
    """Project the SDK plan summary's per-task criterion references.

    The edges come from ``PlanSummary.task_acceptance_refs`` — the same public
    reader that hands over the criteria themselves, so one plan yields one
    graph. MCP deliberately does NOT read the durable ``parsed_plan.json`` for
    this: splitting a single contract across two readers lets them disagree,
    and a private-file read that degrades to ``[]`` would show fully typed
    criteria beside a silently missing set of owners.

    IDs only: a task never restates criterion text. A task that owns no
    criterion arrives with an empty list and stays visible.
    """
    return [TaskAcceptanceRefsRecord.model_validate(r) for r in refs]


def project_human_decision(record: Any) -> HumanCriterionDecisionRecord:
    """Project one durable human decision onto the wire, keys and all."""
    return HumanCriterionDecisionRecord.model_validate(record)


def list_human_decisions(run_id: str) -> list[HumanCriterionDecisionRecord]:
    """The run's append-only decision log, in durable write order."""
    if _sdk_list_criterion_decisions is None:
        raise InvalidPlanError(
            f"criterion_decisions unavailable: {STALE_CORE_MESSAGE}",
        )
    with map_sdk_errors(run_id):
        return [
            project_human_decision(r)
            for r in _sdk_list_criterion_decisions(run_id, cwd=None)
        ]


# ── next actions (one criterion-aware projection for every surface) ─────────


#: The tool that resolves a pending ``human`` criterion.
CRITERION_DECISION_TOOL = "orcho_criterion_decide"

#: Ready-to-forward calls that would SHIP the run. An open blocking criterion
#: is core's release gap (``pipeline.verification_readiness``
#: ``criterion_release_gaps``), so advertising one of these as ready-to-forward
#: beside it would tell a captain two contradictory things at once.
SHIPPING_TOOLS = frozenset({"orcho_delivery_decide"})


def criterion_next_actions(
    run_id: str, readiness: CriterionMatrixSummaryRecord | None,
) -> list[NextActionRecord]:
    """One operator-input action per criterion still awaiting an operator.

    Forwards core's own verdict rather than inventing a policy: the reducer
    already marked these rows ``blocking`` and put their ids in
    ``pending_human_ids``, and core's release gaps already refuse to ship on
    them. This turns that fact into the call that resolves it, so a captain
    reading any surface is pointed at the one action that can move the run.

    Empty when the run has no criterion contract or nothing is pending.
    """
    if readiness is None:
        return []
    return [
        NextActionRecord(
            intent=(
                f"Record the operator's accept / reject verdict for criterion "
                f"{criterion_id}; it blocks release until decided."
            ),
            tool=CRITERION_DECISION_TOOL,
            args={"run_id": run_id, "criterion_id": criterion_id},
            optional=False,
            kind="operator_input_required",
            requires_operator_input=True,
            choices=["accept", "reject"],
            context={
                "blocked_by": "criterion",
                "criterion_id": criterion_id,
                "criterion_state": "pending",
            },
        )
        for criterion_id in readiness.pending_human_ids
    ]


def gate_actions_on_criteria(
    run_id: str,
    actions: list[NextActionRecord],
    readiness: CriterionMatrixSummaryRecord | None,
) -> list[NextActionRecord]:
    """Lead with pending human decisions and gate shipping on every blocker.

    The single criterion-aware action projection every surface uses, so
    status, diagnose, and delivery cannot disagree about what to do next while
    a blocking criterion is open.

    Two effects, both derived from core's facts and nothing else:

    * pending-decision calls come FIRST when human input can clear a blocker;
    * a shipping ``ready_call`` is demoted to ``operator_input_required``
      rather than deleted. Deleting it would hide a real gate from the
      operator; leaving it ``ready_call`` would invite a captain to forward a
      delivery that core's release gaps will refuse. This applies whenever
      core reports ``ready == false``, including failed, stale, or missing
      executable proof and a human rejection with no pending decision.

    Non-shipping actions (resume, and every read-only inspection) are left
    exactly as they were. A mid-flight run legitimately needs its resume, and
    a criterion gate is a release-boundary concern, not a reason to strand a
    run that has not reached it.
    """
    if readiness is None or readiness.ready:
        return actions
    pending = criterion_next_actions(run_id, readiness)
    pending_human_ids = list(readiness.pending_human_ids)
    gated: list[NextActionRecord] = []
    for action in actions:
        if action.tool in SHIPPING_TOOLS and action.kind == "ready_call":
            context = {
                **(action.context or {}),
                "blocked_by": "criterion",
                "criterion_blocking_open": readiness.blocking_open,
                "criterion_states": dict(readiness.counts_by_state),
                "pending_human_criteria": pending_human_ids,
            }
            if pending_human_ids:
                context["resolve_with"] = CRITERION_DECISION_TOOL
            gated.append(action.model_copy(update={
                "kind": "operator_input_required",
                "requires_operator_input": True,
                "optional": True,
                "context": context,
            }))
        else:
            gated.append(action)
    return [*pending, *gated]


# ── write ────────────────────────────────────────────────────────────────────


def record_human_decision(
    run_id: str,
    criterion_id: str,
    decision: str,
    *,
    note: str | None = None,
    actor: str | None = None,
) -> HumanCriterionDecisionRecord:
    """Delegate one typed operator decision to the core durable writer.

    Every admission rule — unknown criterion, non-human criterion, wrong run,
    invalid payload, conflicting/duplicate decision, immutable-contract
    mismatch — is enforced once, by core, **before** anything is written. MCP
    adds no second validator and no local fallback: a rejected decision leaves
    the durable artifact byte-identical and surfaces as ``InvalidPlanError``.

    The SDK request always requires a concrete ``accept`` / ``reject``; the
    optional-``decision`` shape exists only on the MCP tool so the elicitation
    path is reachable, and it is resolved before this call.
    """
    if _sdk_record_criterion_decision is None:
        raise InvalidPlanError(f"orcho_criterion_decide: {STALE_CORE_MESSAGE}")
    with map_sdk_errors(run_id):
        record = _sdk_record_criterion_decision(
            run_id,
            criterion_id=criterion_id,
            decision=decision,
            note=note,
            actor=actor,
            cwd=None,
        )
    return project_human_decision(record)


__all__ = [
    "STALE_CORE_MESSAGE",
    "criterion_sdk_available",
    "list_human_decisions",
    "project_criterion_matrix",
    "project_criterion_readiness",
    "project_human_decision",
    "project_plan_criteria",
    "project_task_acceptance_refs",
    "read_criterion_readiness",
    "record_human_decision",
]
