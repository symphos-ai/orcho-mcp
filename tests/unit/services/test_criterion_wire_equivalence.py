"""Byte-equivalence of the MCP criterion wire against the core SDK (ADR 0188).

Covers M1-M3 of the criterion task. The durable evidence JSON, ``sdk``'s
projection, and the MCP wire must be the SAME canonical JSON — same field
names, same enum values, same row order, same discriminated ``method`` keys,
same proof refs, same summary, and the same *absent* keys. A Pydantic model
that re-sorts, re-defaults, or nullifies any of that is a wire break even
though every individual value still round-trips.

Fixtures come from the INSTALLED public core SDK
(``sdk.criterion_matrix_example`` / ``sdk.human_decision_chain_example``),
never from ``orcho-core/tests/``: a consumer must be provable against the
package it actually imports.
"""
from __future__ import annotations

import pytest
from sdk import (
    CRITERION_EXAMPLE_NAMES,
    CRITERION_EXAMPLES_VERSION,
    CRITERION_STATE_ORDER,
    canonical_criterion_json,
    criterion_matrix_example,
    human_decision_chain_example,
)

from orcho_mcp.schemas.criteria import (
    CriterionMatrixRecord,
    CriterionState,
    HumanCriterionDecisionRecord,
)

#: The example version this consumer is pinned to. Core bumps it when an
#: example's canonical JSON changes; a bump lands here in the same wave so the
#: pin is a deliberate re-conformance, not a silent drift.
PINNED_EXAMPLES_VERSION = "1"

#: Every matrix example that is a matrix (``absent_matrix`` is ``None``).
PRESENT_EXAMPLES = tuple(n for n in CRITERION_EXAMPLE_NAMES if n != "absent_matrix")


def test_pinned_to_the_published_example_version() -> None:
    assert CRITERION_EXAMPLES_VERSION == PINNED_EXAMPLES_VERSION, (
        "orcho-core bumped CRITERION_EXAMPLES_VERSION: re-verify the "
        "byte-equivalence expectations below and update the pin."
    )


@pytest.mark.parametrize("name", PRESENT_EXAMPLES)
def test_wire_round_trip_is_byte_identical_to_core(name: str) -> None:
    """Every conformance example survives the wire models unchanged."""
    example = criterion_matrix_example(name)

    wire = CriterionMatrixRecord.model_validate(example)

    assert canonical_criterion_json(wire.model_dump(mode="json")) == (
        canonical_criterion_json(example)
    )


def test_state_enum_matches_the_canonical_core_order() -> None:
    """The wire enum is core's state set, in core's order — one source.

    Pinning the ORDER too (not just the set) keeps the published JSON Schema
    enumeration and the ``counts_by_state`` key order telling the same story.
    A state added on the core side fails here instead of silently widening the
    wire to an unmodelled value.
    """
    assert CriterionState.__args__ == CRITERION_STATE_ORDER


def test_mixed_state_preserves_counts_by_state_key_order() -> None:
    """F1: ``counts_by_state`` key order is data and must survive Pydantic.

    ``mixed_state`` deliberately spans all three verification classes and all
    three state groups, so an alphabetical or insertion-order-by-accident
    serializer disagrees here even though it would agree on a smaller sample.
    """
    example = criterion_matrix_example("mixed_state")
    core_keys = list(example["summary"]["counts_by_state"])

    wire = CriterionMatrixRecord.model_validate(example)
    wire_keys = list(wire.model_dump(mode="json")["summary"]["counts_by_state"])

    # The example is only load-bearing if it actually spans the groups.
    assert {"proven", "failed", "advisory", "accepted", "pending"} <= set(core_keys)
    assert wire_keys == core_keys
    # …and that order is the canonical one, not merely "whatever core emitted".
    assert wire_keys == [s for s in CRITERION_STATE_ORDER if s in core_keys]
    assert wire_keys != sorted(wire_keys), (
        "the fixture must not be accidentally alphabetical, or this test "
        "cannot tell a sorting serializer from a faithful one"
    )
    assert canonical_criterion_json(wire.summary.model_dump(mode="json")) == (
        canonical_criterion_json(example["summary"])
    )


def test_mixed_state_covers_all_three_verification_classes() -> None:
    """M3's fixture premise: three classes, states from all three groups."""
    wire = CriterionMatrixRecord.model_validate(
        criterion_matrix_example("mixed_state"),
    )

    assert {row.verify for row in wire.rows} == {
        "executable", "agent_assertion", "human",
    }
    assert {row.method.kind for row in wire.rows} == {
        "gates", "inspection", "manual",
    }


def test_discriminated_method_omits_the_other_arms_keys() -> None:
    """``gate_refs`` is absent outside ``gates``; ``instructions`` outside ``manual``."""
    wire = CriterionMatrixRecord.model_validate(
        criterion_matrix_example("three_class"),
    )
    dumped = wire.model_dump(mode="json")

    by_kind = {row["method"]["kind"]: row["method"] for row in dumped["rows"]}
    assert set(by_kind["gates"]) == {"kind", "gate_refs"}
    assert set(by_kind["inspection"]) == {"kind"}
    assert set(by_kind["manual"]) == {"kind", "instructions"}


def test_multi_gate_row_keeps_every_gate_identity() -> None:
    """A gate identity is ``(command, hook, phase)`` — never a bare command."""
    wire = CriterionMatrixRecord.model_validate(
        criterion_matrix_example("multi_gate"),
    )

    row = wire.rows[0]
    assert row.method.kind == "gates"
    assert len(row.method.gate_refs) == 3
    for ref in row.method.gate_refs:
        assert ref.command
        assert ref.hook


def test_explicit_empty_matrix_is_present_and_ready() -> None:
    """A criteria-less new-format plan is an empty matrix, not an absent one."""
    example = criterion_matrix_example("explicit_empty")
    assert example is not None

    wire = CriterionMatrixRecord.model_validate(example)

    assert wire.rows == []
    assert wire.summary.total == 0
    assert wire.summary.blocking_open == 0
    assert wire.summary.ready is True
    assert wire.summary.counts_by_state == {}
    assert wire.summary.pending_human_ids == []


def test_absent_matrix_example_is_none_not_empty() -> None:
    """The legacy case core models as ``None`` — distinct from empty."""
    assert criterion_matrix_example("absent_matrix") is None


def test_readiness_invariant_is_forwarded_not_recomputed() -> None:
    """``ready == (blocking_open == 0)`` holds on every example, from core."""
    for name in PRESENT_EXAMPLES:
        wire = CriterionMatrixRecord.model_validate(criterion_matrix_example(name))
        assert wire.summary.ready == (wire.summary.blocking_open == 0), name


def test_agent_assertion_is_never_proven() -> None:
    """An inspected criterion is advisory at best — never proof."""
    for name in PRESENT_EXAMPLES:
        wire = CriterionMatrixRecord.model_validate(criterion_matrix_example(name))
        for row in wire.rows:
            if row.verify == "agent_assertion":
                assert row.state != "proven", (name, row.criterion_id)
                assert row.blocking is False, (name, row.criterion_id)


# ── F2: durable human decisions ──────────────────────────────────────────────


def test_human_decision_chain_is_byte_identical_to_core() -> None:
    """Both chain records — one with optional fields, one without."""
    for record in human_decision_chain_example():
        wire = HumanCriterionDecisionRecord.model_validate(record)
        assert canonical_criterion_json(wire.model_dump(mode="json")) == (
            canonical_criterion_json(record)
        )


def test_unused_optional_decision_keys_are_omitted_never_null() -> None:
    """F2: absent means the key is gone, not that it carries ``null``."""
    first, replacement = human_decision_chain_example()

    with_optionals = HumanCriterionDecisionRecord.model_validate(first).model_dump()
    without = HumanCriterionDecisionRecord.model_validate(replacement).model_dump()

    # The fixture is only meaningful if the two records differ in what they use.
    assert {"note", "actor"} <= set(with_optionals)
    assert "supersedes" not in with_optionals
    assert "supersedes" in without
    assert "note" not in without
    assert "actor" not in without
    assert None not in with_optionals.values()
    assert None not in without.values()


def test_recorded_at_is_carried_opaquely() -> None:
    """F2: the timestamp is core's string, never reparsed or reformatted.

    The fractional-second record is the one a datetime round-trip would
    normalize (dropping ``.500000`` or re-rendering the offset), so it proves
    the pass-through rather than merely agreeing by luck.
    """
    _, replacement = human_decision_chain_example()
    assert replacement["recorded_at"] == "2026-01-01T00:05:00.500000Z"

    wire = HumanCriterionDecisionRecord.model_validate(replacement)

    assert wire.recorded_at == replacement["recorded_at"]
    assert wire.model_dump()["recorded_at"] == replacement["recorded_at"]


def test_decision_id_is_what_a_proof_ref_cites() -> None:
    """The head decision's id is the ``human_decision`` proof-ref target."""
    chain = human_decision_chain_example()
    head = HumanCriterionDecisionRecord.model_validate(chain[-1])

    assert head.supersedes == chain[0]["decision_id"]
    assert head.decision_id != head.supersedes

    matrix = CriterionMatrixRecord.model_validate(
        criterion_matrix_example("mixed_state"),
    )
    accepted = [r for r in matrix.rows if r.state == "accepted"]
    assert accepted, "mixed_state must carry an accepted human row"
    for row in accepted:
        kinds = {ref.kind for ref in row.proof_refs}
        assert kinds == {"human_decision"}
        assert len(row.proof_refs) == 1
