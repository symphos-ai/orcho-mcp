"""orcho_mcp.schemas.criteria — wire models for typed acceptance criteria.

Home of the ADR 0188 criterion contract on the MCP wire: typed plan
criteria, the criterion matrix (rows + summary), typed human decisions, and
the two result shapes of the criterion-decision action.

**MCP is a pure consumer.** orcho-core owns criterion identity, the
verification classes, the state algebra, receipt freshness, gate selection,
executors, blocking consequences, readiness, and the durable decision log.
Every model here is a *projection* of a core SDK payload; nothing in this
package recomputes a state, a readiness verdict, or an executor.

Three serialization rules are load-bearing, because the durable evidence
JSON, the core SDK projection, and this wire shape must be byte-identical
under canonical JSON (``sdk.canonical_criterion_json``):

1. **Field order is data.** Pydantic dumps in field-definition order, so
   every model below declares its fields in core's durable key order. The
   ``counts_by_state`` mapping carries the canonical state order
   (``sdk.CRITERION_STATE_ORDER``) as *insertion order* — it must never be
   re-sorted, which is why it stays a plain ``dict[str, int]``.
2. **Absent is not empty and never ``null``.** ``method.gate_refs`` outside
   ``gates``, ``method.instructions`` outside ``manual``, a criterion's
   class-irrelevant key, an unused ``note`` / ``actor`` / ``supersedes``,
   and a legacy run's whole ``criterion_matrix`` are *absent* keys. The
   explicit dump policy is :func:`omit_absent_keys`; the matching JSON
   Schema policy is :func:`non_nullable_optional`, which publishes those
   properties as optional and non-nullable rather than ``anyOf[X, null]``.
3. **Opaque scalars stay opaque.** ``recorded_at`` is core's canonical
   RFC 3339 UTC string. It is carried as ``str`` and is never reparsed,
   re-formatted, or re-normalized on the MCP side.
4. **Required stays required, and unknown is refused.** Every key the
   interface contract calls mandatory is declared without a default, every
   documented non-emptiness rule is a real constraint, and every exact
   criterion model forbids extra keys (:data:`STRICT`). Pydantic's default is
   to *drop* an unmodelled key, which would make MCP quietly repair a
   malformed payload: ``{"kind": "inspection", "gate_refs": [...]}`` would
   validate and then serialize without the ``gate_refs`` it was handed. That
   is the byte-equivalence contract broken in the most invisible way
   available. A contract break must fail loudly at the boundary rather than
   travel onward wearing valid clothes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from orcho_mcp.schemas.shared import NextActionRecord

# ── serialization / schema policy ────────────────────────────────────────────


def non_nullable_optional(schema: dict[str, Any]) -> None:
    """JSON Schema policy for a key that is *absent*, never ``null``.

    Pydantic renders ``X | None = None`` as ``anyOf: [X, {"type": "null"}]``
    with ``default: null`` — which advertises exactly the wire shape ADR 0188
    forbids. This callable (passed as ``json_schema_extra``) collapses the
    union back to ``X`` and drops the ``null`` default, so the published
    contract reads "optional property, object/string when present".

    Pair it with :func:`omit_absent_keys` on the owning model; the schema
    policy alone would describe a shape the serializer does not produce.
    """
    any_of = schema.pop("anyOf", None)
    if any_of:
        keep = [s for s in any_of if s.get("type") != "null"]
        if len(keep) == 1:
            schema.update(keep[0])
        else:  # pragma: no cover - defensive; no multi-arm union uses this
            schema["anyOf"] = keep
    if schema.get("default", ...) is None:
        schema.pop("default")


def omit_absent_keys(*names: str):
    """Build a wrap serializer that drops the named keys when they are ``None``.

    The explicit dump policy behind rule 2 in the module docstring. It is
    deliberately *per-key* rather than a blanket ``exclude_none``: a model may
    legitimately carry a nullable field whose ``null`` is meaningful, and a
    global flag on a containing model would silently strip unrelated slices.
    """

    def _serializer(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        for name in names:
            if data.get(name, ...) is None:
                data.pop(name, None)
        return data

    return model_serializer(mode="wrap")(_serializer)


#: Shared config for every model that mirrors an EXACT core payload shape.
#: ``extra="forbid"`` is the load-bearing half: without it an unmodelled key
#: is silently dropped, so a malformed payload would be *repaired* into a
#: valid-looking one instead of rejected — and the byte-equivalence the whole
#: contract rests on would fail silently rather than loudly.
STRICT = ConfigDict(extra="forbid")

#: The criterion id grammar (ADR 0188). Mirrors
#: ``core.contracts.criteria.CRITERION_ID_RE`` as a wire constraint so a
#: malformed id fails at the boundary instead of reaching a client that will
#: use it as a stable key.
CRITERION_ID_PATTERN = r"^C[1-9][0-9]*$"

#: Every criterion state, in core's canonical serialization order. Mirrors
#: ``sdk.CRITERION_STATE_ORDER`` as a closed wire enum so clients can branch on
#: a known set; ``tests/unit/inspection`` pins the two together, so a core-side
#: addition fails loudly here instead of silently widening the wire.
CriterionState = Literal[
    "proven",
    "failed",
    "stale",
    "missing",
    "not_selected",
    "advisory",
    "accepted",
    "rejected",
    "pending",
]

#: The canonical serialization order as a sequence, derived from the enum
#: above so the two cannot drift. ``test_criterion_wire_equivalence`` pins the
#: enum itself to ``sdk.CRITERION_STATE_ORDER``.
CRITERION_STATE_ORDER: tuple[str, ...] = CriterionState.__args__

#: The three verification classes — ADR 0188 §2.
CriterionVerify = Literal["executable", "agent_assertion", "human"]

#: What a proof reference points at.
CriterionProofKind = Literal["receipt", "finding", "claim", "human_decision"]

#: The operator verdict on a ``human`` criterion.
CriterionDecisionVerb = Literal["accept", "reject"]


# ── typed plan criteria ──────────────────────────────────────────────────────


class CriterionGateRefRecord(BaseModel):
    """The complete scheduled-gate identity a criterion refers to.

    All three keys are always present. A command name alone is NOT a gate
    identity: the same command scheduled under one hook for two phases is two
    distinct official gates. ``phase`` is non-empty only for the phase-anchored
    hooks (``before_phase`` / ``after_phase``); ``before_delivery`` /
    ``on_resume`` / ``manual_only`` carry ``""``, matching the durable ledger.
    """

    model_config = STRICT

    command: str = Field(min_length=1)
    hook: str = Field(min_length=1)
    # The only deliberately empty-able field on the wire: a non-phase-anchored
    # hook (``before_delivery`` / ``on_resume`` / ``manual_only``) carries
    # ``""``, exactly as the durable ledger keys it.
    phase: str


class PlanCriterionRecord(BaseModel):
    """One typed plan acceptance criterion, in its durable key order.

    Projected verbatim from the core SDK plan summary — MCP never stringifies
    a criterion, invents an ID, or reclassifies one. Legacy ``list[str]``
    plans are normalized by core's single ingress normalizer *before* they
    reach this model, so ``acceptance_criteria`` never carries bare strings.

    Class-irrelevant keys are absent: ``gate_refs`` only for ``executable``
    (non-empty), ``human_instructions`` only for ``human`` (non-empty),
    neither for ``agent_assertion``. Those are validated, not merely
    documented — an ``executable`` criterion with no gate identity names no
    proof at all, and admitting one would let the plan slice advertise
    traceability the run cannot deliver.
    """

    model_config = STRICT

    id: str = Field(
        pattern=CRITERION_ID_PATTERN,
        description="Stable criterion id in the ``C<n>`` grammar (ADR 0188).",
    )
    intent: str = Field(min_length=1)
    verify: CriterionVerify
    gate_refs: Annotated[
        list[CriterionGateRefRecord] | None,
        Field(default=None, json_schema_extra=non_nullable_optional),
    ] = None
    human_instructions: Annotated[
        str | None,
        Field(default=None, json_schema_extra=non_nullable_optional),
    ] = None

    _omit = omit_absent_keys("gate_refs", "human_instructions")

    @model_validator(mode="after")
    def _class_invariants(self) -> PlanCriterionRecord:
        """Exactly one class, exactly the keys that class admits."""
        if self.verify == "executable":
            if not self.gate_refs:
                raise ValueError(
                    f"criterion {self.id}: an 'executable' criterion must name "
                    "at least one gate identity in gate_refs",
                )
            if self.human_instructions is not None:
                raise ValueError(
                    f"criterion {self.id}: 'executable' criteria carry no "
                    "human_instructions",
                )
        elif self.verify == "human":
            if not (self.human_instructions or "").strip():
                raise ValueError(
                    f"criterion {self.id}: a 'human' criterion must carry "
                    "non-empty human_instructions",
                )
            if self.gate_refs is not None:
                raise ValueError(
                    f"criterion {self.id}: 'human' criteria carry no gate_refs",
                )
        else:  # agent_assertion — neither key
            if self.gate_refs is not None or self.human_instructions is not None:
                raise ValueError(
                    f"criterion {self.id}: 'agent_assertion' criteria carry "
                    "neither gate_refs nor human_instructions",
                )
        return self


class TaskAcceptanceRefsRecord(BaseModel):
    """One plan task's references to plan criteria, by ID only.

    A task never restates criterion text; ``acceptance_refs`` are IDs that
    resolve against :class:`PlanCriterionRecord` entries in the same plan.

    One entry per plan task, including a task that references no criterion —
    dropping those would give a client a partial reference graph and no way
    to tell "this task owns nothing" from "this task is missing".
    """

    model_config = STRICT

    task_id: str = Field(min_length=1)
    acceptance_refs: list[str] = Field(default_factory=list)


# ── criterion matrix ─────────────────────────────────────────────────────────


class CriterionMethodGates(BaseModel):
    """``executable`` — proved by the named official gates."""

    model_config = STRICT

    kind: Literal["gates"] = "gates"
    gate_refs: list[CriterionGateRefRecord] = Field(min_length=1)


class CriterionMethodInspection(BaseModel):
    """``agent_assertion`` — inspected by an agent. Advisory, never proof."""

    model_config = STRICT

    kind: Literal["inspection"] = "inspection"


class CriterionMethodManual(BaseModel):
    """``human`` — decided by an operator following ``instructions``."""

    model_config = STRICT

    kind: Literal["manual"] = "manual"
    instructions: str = Field(min_length=1)


#: Discriminated on ``kind``. Each arm declares only its own keys, so
#: ``gate_refs`` is genuinely absent outside ``gates`` and ``instructions``
#: outside ``manual`` — no empty-list or ``null`` placeholder is ever emitted.
CriterionMethod = Annotated[
    CriterionMethodGates | CriterionMethodInspection | CriterionMethodManual,
    Field(discriminator="kind"),
]


class CriterionProofRefRecord(BaseModel):
    """One pointer to the durable artefact that supports a row's state."""

    model_config = STRICT

    kind: CriterionProofKind
    id: str = Field(min_length=1)


class CriterionRowRecord(BaseModel):
    """One criterion's row of the matrix — exactly one row per plan criterion.

    Every field is core's, in core's order. ``executors`` arrives non-empty
    from the SDK; MCP never assigns ``reviewer`` / ``human`` / a task ID
    itself. ``state``, ``reason``, and ``blocking`` are the reducer's verdict:
    no MCP module recomputes them, and no developer claim, reviewer finding,
    transcript command, or command-name-only match can turn a row ``proven``.
    """

    model_config = STRICT

    criterion_id: str = Field(pattern=CRITERION_ID_PATTERN)
    intent: str = Field(min_length=1)
    verify: CriterionVerify
    executors: list[str] = Field(
        min_length=1,
        description=(
            "The task ids (or the class's canonical executor) that own this "
            "criterion, in a deterministic order. Never empty: every "
            "criterion has an owner, and an empty list would read as "
            "'nobody is responsible'."
        ),
    )
    method: CriterionMethod
    proof_refs: list[CriterionProofRefRecord]
    state: CriterionState
    reason: str
    blocking: bool


class CriterionMatrixSummaryRecord(BaseModel):
    """The reducer's aggregate verdict for a run.

    ``ready == (blocking_open == 0)`` is core's invariant, forwarded — not
    re-derived here. ``counts_by_state`` holds only present states with
    positive counts, keyed in ``sdk.CRITERION_STATE_ORDER``; it is a plain
    mapping precisely so its **insertion order survives** serialization.
    """

    model_config = STRICT

    total: int = Field(ge=0)
    blocking_open: int = Field(ge=0)
    ready: bool
    counts_by_state: dict[CriterionState, PositiveInt] = Field(
        description=(
            "Counts for the states PRESENT in the rows, keyed in core's "
            "canonical state order. Only present states appear, and only with "
            "a positive count — a zero entry and an unknown state name are "
            "both contract breaks, not merely unusual."
        ),
    )
    pending_human_ids: list[str]

    @model_validator(mode="after")
    def _canonical_counts_order(self) -> CriterionMatrixSummaryRecord:
        """Reject counts whose key order is not the canonical one.

        ``counts_by_state`` key order is *data*: it is the one place the wire
        carries ``CRITERION_STATE_ORDER``, and a consumer rendering the states
        in payload order depends on it. A dict that round-trips every value
        correctly but in a different order has silently lost that meaning, and
        nothing downstream would notice — which is exactly why it is checked
        here rather than trusted.
        """
        present = list(self.counts_by_state)
        expected = [s for s in CRITERION_STATE_ORDER if s in self.counts_by_state]
        if present != expected:
            raise ValueError(
                f"counts_by_state keys {present} are not in the canonical "
                f"state order; expected {expected}",
            )
        return self

    @model_validator(mode="after")
    def _readiness_invariant(self) -> CriterionMatrixSummaryRecord:
        """Reject a summary whose ``ready`` contradicts ``blocking_open``.

        This is NOT a re-derivation: ``ready`` is still core's value, taken
        as given. It is a consistency check on a payload that claims to be
        core's — a summary saying "ready" while naming open blockers is a
        contract break, and forwarding it would let a captain ship on it.
        """
        if self.blocking_open > self.total:
            raise ValueError(
                f"blocking_open ({self.blocking_open}) exceeds total "
                f"({self.total})",
            )
        if self.ready != (self.blocking_open == 0):
            raise ValueError(
                f"ready ({self.ready}) contradicts blocking_open "
                f"({self.blocking_open}); ready means blocking_open == 0",
            )
        return self


class CriterionMatrixRecord(BaseModel):
    """The criterion matrix: one row per plan criterion, plus the summary.

    Absent vs empty is meaningful. A run predating the contract (or with no
    accepted plan) has **no matrix at all** — the SDK returns ``None`` and the
    owning wire model omits the key entirely. A new-format plan that declares
    no criteria has an explicit empty matrix: ``rows: []`` with the exact
    zeroed summary. ``null`` is never emitted for either case.
    """

    model_config = STRICT

    rows: list[CriterionRowRecord]
    summary: CriterionMatrixSummaryRecord


#: Shared annotation for the criterion-readiness field carried by
#: ``orcho_run_status``, ``orcho_run_diagnose``, and ``orcho_delivery_gate``.
#: Declared once so those surfaces cannot drift in shape or in wording, and
#: because they all read the value from the *same* projection call — that is
#: what makes them agree on blockers and next action by construction.
#:
#: Absent (key omitted, never ``null``) for a run with no criterion contract.
CriterionReadinessField = Annotated[
    CriterionMatrixSummaryRecord | None,
    Field(
        default=None,
        json_schema_extra=non_nullable_optional,
        description=(
            "ADR 0188 criterion readiness, forwarded from core's reducer: "
            "``total`` / ``blocking_open`` / ``ready`` / ``counts_by_state`` "
            "(canonical state order) / ``pending_human_ids``. Never "
            "recomputed on the MCP side. The full rows live on "
            "``orcho_run_evidence`` slice ``criterion_matrix``. The key is "
            "omitted for a run with no criterion contract."
        ),
    ),
]


# ── typed human decisions ────────────────────────────────────────────────────


class HumanCriterionDecisionRecord(BaseModel):
    """One durable operator decision on a ``human`` criterion.

    The durable JSON shape, forwarded field for field:

    * ``decision_id`` is writer-assigned and is exactly what a
      ``{"kind": "human_decision"}`` proof ref cites;
    * ``recorded_at`` is core's canonical RFC 3339 UTC string
      (``YYYY-MM-DDTHH:MM:SS[.ffffff]Z``), carried **opaquely** — MCP never
      parses, re-formats, or re-normalizes it;
    * ``note`` / ``actor`` / ``supersedes`` are absent when unused. Core
      never writes ``null`` for them and rejects ``null`` on read, so the
      wire omits the key rather than emitting one.

    Supersession is core's: the log is append-only, the first decision omits
    ``supersedes``, and a later one names the current head. Stale and branched
    chains are rejected by the durable writer before anything is written.
    """

    model_config = STRICT

    decision_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    criterion_id: str = Field(pattern=CRITERION_ID_PATTERN)
    decision: CriterionDecisionVerb
    recorded_at: str = Field(
        min_length=1,
        description=(
            "Core's canonical RFC 3339 UTC timestamp "
            "(YYYY-MM-DDTHH:MM:SS[.ffffff]Z). Opaque — do not reparse."
        ),
    )
    # Optional, but never empty when present: core rejects a blank optional
    # before writing, so a blank one arriving here is a contract break.
    note: Annotated[
        str | None,
        Field(default=None, min_length=1, json_schema_extra=non_nullable_optional),
    ] = None
    actor: Annotated[
        str | None,
        Field(default=None, min_length=1, json_schema_extra=non_nullable_optional),
    ] = None
    supersedes: Annotated[
        str | None,
        Field(default=None, min_length=1, json_schema_extra=non_nullable_optional),
    ] = None

    _omit = omit_absent_keys("note", "actor", "supersedes")

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_nulls(cls, data: Any) -> Any:
        """An unused optional is an ABSENT key — an explicit ``null`` is a break.

        Core never writes ``null`` for these and rejects it on read, so a
        payload carrying one did not come from a conforming writer. Accepting
        it would let the two spellings of "unused" diverge on the wire, and a
        client branching on key presence would then be wrong. ``mode="before"``
        is what makes the distinction possible at all: after coercion, absent
        and ``null`` are the same ``None``.
        """
        if isinstance(data, Mapping):
            nulls = sorted(
                key for key in ("note", "actor", "supersedes")
                if key in data and data[key] is None
            )
            if nulls:
                raise ValueError(
                    f"{nulls} must be omitted when unused, never null",
                )
        return data


# ── orcho_criterion_decide ───────────────────────────────────────────────────


class CriterionDecisionRecordedResult(BaseModel):
    """Returned when a typed decision was recorded by core.

    ``outcome`` reports the DURABLE fact and nothing else: reaching this
    model means the decision is written. ``matrix`` is a convenience readback
    taken *after* the write, through the same single projection path every
    other criterion surface uses, so a caller normally sees the readiness
    transition its decision caused without a second tool call.

    The two are deliberately decoupled. A readback that fails — an unreadable
    artifact, a version-skewed payload — must NOT be reported as a failed
    decision: the journal has already changed, a retry would be refused as a
    duplicate, and the operator would have no way to tell a lost write from a
    recorded one. In that case ``matrix`` is absent and ``matrix_error`` names
    why, so the caller re-reads it with ``orcho_run_evidence``
    (``slice="criterion_matrix"``) instead of re-deciding.
    """

    outcome: Literal["decision_recorded"] = "decision_recorded"
    run_id: str
    criterion_id: str
    decision: HumanCriterionDecisionRecord
    matrix: Annotated[
        CriterionMatrixRecord | None,
        Field(default=None, json_schema_extra=non_nullable_optional),
    ] = None
    matrix_error: Annotated[
        str | None,
        Field(
            default=None,
            min_length=1,
            json_schema_extra=non_nullable_optional,
            description=(
                "Why the post-write matrix readback was unavailable. Present "
                "ONLY together with an absent ``matrix``; the decision itself "
                "is recorded either way. Re-read the matrix with "
                "``orcho_run_evidence`` slice='criterion_matrix' — do not "
                "re-issue the decision."
            ),
        ),
    ] = None

    _omit = omit_absent_keys("matrix", "matrix_error")


class CriterionDecisionInputRequiredResult(BaseModel):
    """Returned when the operator's verdict is still missing.

    Reached only when ``decision`` was omitted and the client does not
    advertise MCP form elicitation. **Nothing is written.** The record carries
    the exact missing-input schema and a complete ready-call, so the caller
    collects one explicit ``accept`` / ``reject`` from a human and replays the
    call. A decision is never inferred from chat prose.
    """

    outcome: Literal["operator_input_required"] = "operator_input_required"
    run_id: str
    criterion_id: str
    reason: str
    instructions: Annotated[
        str | None,
        Field(
            default=None,
            json_schema_extra=non_nullable_optional,
            description=(
                "The criterion's own ``human_instructions`` — what the "
                "operator must actually do before deciding. Absent when the "
                "criterion could not be read."
            ),
        ),
    ] = None
    next_actions: list[NextActionRecord] = Field(default_factory=list)

    _omit = omit_absent_keys("instructions")


__all__ = [
    "CRITERION_ID_PATTERN",
    "STRICT",
    "CRITERION_STATE_ORDER",
    "CriterionDecisionInputRequiredResult",
    "CriterionDecisionRecordedResult",
    "CriterionDecisionVerb",
    "CriterionGateRefRecord",
    "CriterionMatrixRecord",
    "CriterionMatrixSummaryRecord",
    "CriterionMethod",
    "CriterionMethodGates",
    "CriterionMethodInspection",
    "CriterionMethodManual",
    "CriterionProofKind",
    "CriterionProofRefRecord",
    "CriterionReadinessField",
    "CriterionRowRecord",
    "CriterionState",
    "CriterionVerify",
    "HumanCriterionDecisionRecord",
    "PlanCriterionRecord",
    "TaskAcceptanceRefsRecord",
    "non_nullable_optional",
    "omit_absent_keys",
]
