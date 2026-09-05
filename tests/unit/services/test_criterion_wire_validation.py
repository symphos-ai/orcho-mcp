"""The criterion wire refuses payloads the interface contract forbids.

Byte-equivalence proves MCP does not *change* a good payload. This file
proves the other half: MCP does not *repair* a bad one. A permissive model
would launder a truncated or malformed SDK payload into something that looks
valid on the wire — a row with no owner, a summary claiming readiness while naming open blockers — and the
client has no way to tell. Every case below must fail loudly at the boundary.

The base payloads are the installed core SDK's own conformance examples, so
each test mutates a *known-good* shape by exactly one field. That keeps the
tests honest about what they forbid: if a rejection here ever contradicts
what core actually emits, the equivalence tests in
``test_criterion_wire_equivalence.py`` fail first.
"""
from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError
from sdk import criterion_matrix_example, human_decision_chain_example

from orcho_mcp.schemas.criteria import (
    CRITERION_STATE_ORDER,
    CriterionMatrixRecord,
    CriterionMatrixSummaryRecord,
    CriterionRowRecord,
    CriterionState,
    HumanCriterionDecisionRecord,
    PlanCriterionRecord,
)


def _matrix(name: str = "three_class") -> dict[str, Any]:
    return copy.deepcopy(criterion_matrix_example(name))


def _row(criterion_id: str = "C1") -> dict[str, Any]:
    return next(
        r for r in _matrix()["rows"] if r["criterion_id"] == criterion_id
    )


def _summary() -> dict[str, Any]:
    return copy.deepcopy(_matrix()["summary"])


def _decision() -> dict[str, Any]:
    return copy.deepcopy(human_decision_chain_example()[0])


# ── typed plan criteria ──────────────────────────────────────────────────────

_GATE = {"command": "unit", "hook": "after_phase", "phase": "implement"}
_EXECUTABLE = {
    "id": "C1", "intent": "proved by a gate", "verify": "executable",
    "gate_refs": [_GATE],
}
_HUMAN = {
    "id": "C1", "intent": "decided by an operator", "verify": "human",
    "human_instructions": "Exercise it and record the outcome.",
}
_ASSERTION = {"id": "C1", "intent": "read it", "verify": "agent_assertion"}


def test_the_three_well_formed_classes_validate() -> None:
    """The premise: each class's own shape is accepted."""
    for payload in (_EXECUTABLE, _HUMAN, _ASSERTION):
        assert PlanCriterionRecord.model_validate(payload).verify == payload["verify"]


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        (
            {**_EXECUTABLE, "human_instructions": "not for this class"},
            "executable carries no human_instructions",
        ),
        (
            {**_HUMAN, "human_instructions": "   "},
            "human instructions must be real instructions",
        ),
        (
            {k: v for k, v in _HUMAN.items() if k != "human_instructions"},
            "human requires instructions",
        ),
        ({**_HUMAN, "gate_refs": [_GATE]}, "human carries no gate_refs"),
        ({**_ASSERTION, "gate_refs": [_GATE]}, "agent_assertion carries neither"),
        (
            {**_ASSERTION, "human_instructions": "x"},
            "agent_assertion carries neither",
        ),
        ({**_ASSERTION, "id": "C0"}, "C0 is outside the id grammar"),
        ({**_ASSERTION, "id": "criterion-1"}, "prose is not an id"),
        ({**_ASSERTION, "intent": ""}, "a criterion without intent says nothing"),
        ({**_ASSERTION, "verify": "manual"}, "there are exactly three classes"),
        (
            {**_EXECUTABLE, "gate_refs": [{"command": "unit", "hook": "after_phase"}]},
            "a gate identity is all three keys",
        ),
        (
            {**_EXECUTABLE, "gate_refs": [{**_GATE, "command": ""}]},
            "a gate identity needs a command",
        ),
        (
            {**_EXECUTABLE, "gate_refs": [{**_GATE, "hook": ""}]},
            "a gate identity needs a hook",
        ),
    ],
)
def test_malformed_plan_criterion_is_refused(payload: dict, why: str) -> None:
    with pytest.raises(ValidationError):
        PlanCriterionRecord.model_validate(payload)


def test_an_empty_phase_is_still_a_valid_gate_identity() -> None:
    """The one deliberately empty-able field: a non-phase-anchored hook."""
    record = PlanCriterionRecord.model_validate({
        **_EXECUTABLE,
        "gate_refs": [{"command": "smoke", "hook": "before_delivery", "phase": ""}],
    })

    assert record.gate_refs is not None
    assert record.gate_refs[0].phase == ""


# ── matrix rows ──────────────────────────────────────────────────────────────


def test_the_example_row_validates() -> None:
    assert CriterionRowRecord.model_validate(_row()).criterion_id == "C1"


@pytest.mark.parametrize(
    "missing",
    ["criterion_id", "intent", "verify", "executors", "method", "proof_refs",
     "state", "reason", "blocking"],
)
def test_every_row_key_is_required(missing: str) -> None:
    """The contract calls all nine mandatory — none may be defaulted in."""
    payload = {k: v for k, v in _row().items() if k != missing}

    with pytest.raises(ValidationError):
        CriterionRowRecord.model_validate(payload)


@pytest.mark.parametrize(
    ("mutation", "why"),
    [
        ({"executors": []}, "every criterion has an owner"),
        ({"state": "unknown"}, "state is a closed set"),
        ({"criterion_id": "nope"}, "row ids follow the same grammar"),
        ({"method": {"kind": "gates", "gate_refs": []}}, "gates without a gate"),
        ({"method": {"kind": "manual", "instructions": ""}}, "manual without text"),
        ({"method": {"kind": "elsewhere"}}, "method is discriminated"),
        (
            {"proof_refs": [{"kind": "receipt", "id": ""}]},
            "a proof ref must point somewhere",
        ),
        (
            {"proof_refs": [{"kind": "vibes", "id": "x"}]},
            "proof kinds are a closed set",
        ),
    ],
)
def test_malformed_row_is_refused(mutation: dict, why: str) -> None:
    with pytest.raises(ValidationError):
        CriterionRowRecord.model_validate({**_row(), **mutation})


# ── summary ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "missing",
    ["total", "blocking_open", "ready", "counts_by_state", "pending_human_ids"],
)
def test_every_summary_key_is_required(missing: str) -> None:
    payload = {k: v for k, v in _summary().items() if k != missing}

    with pytest.raises(ValidationError):
        CriterionMatrixSummaryRecord.model_validate(payload)


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        (
            {"total": -1, "blocking_open": 0, "ready": True,
             "counts_by_state": {}, "pending_human_ids": []},
            "a count cannot be negative",
        ),
        (
            {"total": 3, "blocking_open": -1, "ready": True,
             "counts_by_state": {}, "pending_human_ids": []},
            "a count cannot be negative",
        ),
        (
            {"total": 1, "blocking_open": 2, "ready": False,
             "counts_by_state": {}, "pending_human_ids": []},
            "more blockers than criteria is impossible",
        ),
        (
            {"total": 3, "blocking_open": 2, "ready": True,
             "counts_by_state": {}, "pending_human_ids": []},
            "ready while naming open blockers is the dangerous one",
        ),
        (
            {"total": 3, "blocking_open": 0, "ready": False,
             "counts_by_state": {}, "pending_human_ids": []},
            "not-ready with no blockers is equally inconsistent",
        ),
    ],
)
def test_inconsistent_summary_is_refused(payload: dict, why: str) -> None:
    with pytest.raises(ValidationError):
        CriterionMatrixSummaryRecord.model_validate(payload)


def test_matrix_requires_both_rows_and_summary() -> None:
    """A matrix cannot be conjured from a partial payload."""
    for payload in ({}, {"rows": []}, {"summary": _summary()}):
        with pytest.raises(ValidationError):
            CriterionMatrixRecord.model_validate(payload)


# ── human decisions ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "missing", ["decision_id", "run_id", "criterion_id", "decision", "recorded_at"],
)
def test_every_decision_key_is_required(missing: str) -> None:
    payload = {k: v for k, v in _decision().items() if k != missing}

    with pytest.raises(ValidationError):
        HumanCriterionDecisionRecord.model_validate(payload)


@pytest.mark.parametrize(
    ("mutation", "why"),
    [
        ({"decision_id": ""}, "a proof ref target cannot be blank"),
        ({"run_id": ""}, "a decision names its run"),
        ({"criterion_id": "C0"}, "criterion ids follow the grammar"),
        ({"decision": "maybe"}, "the verdict is accept or reject"),
        ({"recorded_at": ""}, "the timestamp is a real value"),
        ({"note": None}, "an unused optional is ABSENT, never null"),
        ({"actor": None}, "an unused optional is ABSENT, never null"),
        ({"supersedes": None}, "an unused optional is ABSENT, never null"),
        ({"note": ""}, "a present optional is non-empty"),
        ({"actor": ""}, "a present optional is non-empty"),
    ],
)
def test_malformed_decision_is_refused(mutation: dict, why: str) -> None:
    with pytest.raises(ValidationError):
        HumanCriterionDecisionRecord.model_validate({**_decision(), **mutation})


def test_a_decision_without_optionals_validates() -> None:
    """Absent optionals are fine — it is ``null`` that is refused."""
    bare = {
        k: v for k, v in _decision().items()
        if k not in ("note", "actor", "supersedes")
    }

    record = HumanCriterionDecisionRecord.model_validate(bare)

    assert record.note is None
    assert "note" not in record.model_dump()


# ── counts_by_state is a closed contract, not a free mapping ────────────────


def test_the_example_summary_validates() -> None:
    """The premise: a real core summary passes every rule below."""
    assert CriterionMatrixSummaryRecord.model_validate(_summary()).total >= 0


@pytest.mark.parametrize(
    ("counts", "why"),
    [
        ({"bogus": 3}, "an unknown state is not a criterion state"),
        ({"proven": 0}, "only PRESENT states appear, so a zero is a break"),
        ({"proven": -2}, "a count cannot be negative"),
        ({"pending": 1, "proven": 2}, "keys must be in canonical order"),
        ({"advisory": 1, "failed": 1, "proven": 1}, "keys must be in canonical order"),
    ],
)
def test_malformed_counts_by_state_is_refused(counts: dict, why: str) -> None:
    payload = {
        "total": sum(v for v in counts.values() if v > 0) or 1,
        "blocking_open": 0,
        "ready": True,
        "counts_by_state": counts,
        "pending_human_ids": [],
    }
    payload["total"] = max(payload["total"], payload["blocking_open"])

    with pytest.raises(ValidationError):
        CriterionMatrixSummaryRecord.model_validate(payload)


def test_counts_in_canonical_order_are_accepted() -> None:
    """The positive control: the same states, correctly ordered, validate.

    Without this the order test above could be passing for the wrong reason
    (e.g. an unrelated rule rejecting every one of these payloads).
    """
    record = CriterionMatrixSummaryRecord.model_validate({
        "total": 4,
        "blocking_open": 1,
        "ready": False,
        "counts_by_state": {"proven": 2, "advisory": 1, "pending": 1},
        "pending_human_ids": ["C4"],
    })

    assert list(record.counts_by_state) == ["proven", "advisory", "pending"]
    assert list(record.model_dump()["counts_by_state"]) == [
        "proven", "advisory", "pending",
    ]


def test_the_wire_order_constant_matches_the_enum() -> None:
    assert CriterionState.__args__ == CRITERION_STATE_ORDER


# ── unknown keys are refused, never silently dropped ────────────────────────


@pytest.mark.parametrize(
    ("method", "why"),
    [
        (
            {"kind": "inspection", "gate_refs": [_GATE]},
            "an inspected criterion names no gate",
        ),
        (
            {"kind": "inspection", "instructions": "do it"},
            "an inspected criterion carries no operator instructions",
        ),
        (
            {"kind": "manual", "instructions": "do it", "gate_refs": [_GATE]},
            "a manual criterion names no gate",
        ),
        (
            {"kind": "gates", "gate_refs": [_GATE], "instructions": "do it"},
            "a gated criterion carries no operator instructions",
        ),
    ],
)
def test_a_method_arm_refuses_another_arms_keys(method: dict, why: str) -> None:
    """The silent-repair case: without ``extra='forbid'`` these VALIDATE.

    Pydantic's default drops an unmodelled key, so ``{"kind": "inspection",
    "gate_refs": [...]}`` would pass and then serialize without the
    ``gate_refs`` it was handed — MCP repairing a malformed core payload into
    a plausible-looking one, and breaking byte-equivalence in the least
    visible way available.
    """
    with pytest.raises(ValidationError):
        CriterionRowRecord.model_validate({**_row(), "method": method})


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (CriterionRowRecord, "row"),
        (CriterionMatrixSummaryRecord, "summary"),
        (HumanCriterionDecisionRecord, "decision"),
        (PlanCriterionRecord, "criterion"),
    ],
)
def test_no_criterion_model_accepts_an_unknown_key(model, payload: str) -> None:
    base = {
        "row": _row,
        "summary": _summary,
        "decision": _decision,
        "criterion": lambda: dict(_ASSERTION),
    }[payload]()

    with pytest.raises(ValidationError):
        model.model_validate({**base, "surprise": "value"})


def test_a_gate_ref_refuses_an_unknown_key() -> None:
    with pytest.raises(ValidationError):
        PlanCriterionRecord.model_validate({
            **_EXECUTABLE, "gate_refs": [{**_GATE, "selected": True}],
        })


@pytest.mark.parametrize("empty", [False, True])
def test_executable_plan_allows_engine_binding(empty):
    payload = {k: v for k, v in _EXECUTABLE.items() if k != "gate_refs"}
    if empty:
        payload["gate_refs"] = []
    assert PlanCriterionRecord.model_validate(payload).model_dump() == payload


@pytest.mark.parametrize("refs", [[], [_GATE]])
def test_implied_method_survives_wire_projection(refs):
    from orcho_mcp.schemas.criteria import CriterionMethodGates

    method = {"kind": "gates", "gate_refs": refs, "implied": True}
    assert CriterionMethodGates.model_validate(method).model_dump() == method


def test_implied_flag_cannot_be_false():
    from orcho_mcp.schemas.criteria import CriterionMethodGates

    with pytest.raises(ValidationError):
        CriterionMethodGates.model_validate({
            "kind": "gates", "gate_refs": [_GATE], "implied": False,
        })
