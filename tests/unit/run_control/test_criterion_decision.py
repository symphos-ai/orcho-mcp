"""``orcho_criterion_decide`` — M5, M6, M7 and the F2 wire contract.

The tool has one job: turn an operator's explicit ``accept`` / ``reject`` into
the strict core SDK request. Everything here is organised around the two ways
that can go wrong — deciding for the operator, and deciding on something core
would refuse — so each test names the failure it forbids.

Runs are real synthetic run dirs, so the durable decision journal core writes
is the one MCP reads back. That is what makes the "no write" assertions mean
something: they check the artifact, not a mock.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sdk import (
    canonical_criterion_json,
    list_criterion_decisions,
    record_criterion_decision,
)

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.run_control.criterion_decision import (
    CRITERION_DECISION_INPUT_SCHEMA,
    decide_criterion_with_elicitation,
)
from orcho_mcp.services.criterion_projection import (
    list_human_decisions,
    project_criterion_matrix,
)
from tests.fixtures.mcp_workspace import criterion_plan, meta, write_run

RUN = "20260501_000001"
DECISIONS = "criterion_decisions.json"


@pytest.fixture
def run_dir(fake_workspace):
    return write_run(
        fake_workspace, RUN,
        meta=meta(status="done", project=str(fake_workspace), task="t"),
        parsed_plan=criterion_plan(),
    )


def _journal(run_dir) -> str | None:
    path = run_dir / DECISIONS
    return path.read_text(encoding="utf-8") if path.exists() else None


class _Ctx:
    """A client that answers the native form with ``reply``."""

    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[str] = []

    async def elicit(self, *, message, schema):  # noqa: ARG002
        self.prompts.append(message)
        return self.reply


@pytest.fixture
def capable(monkeypatch):
    monkeypatch.setattr(
        "orcho_mcp.run_control.handoff._client_supports_form_elicitation",
        lambda _ctx: True,
    )


@pytest.fixture
def incapable(monkeypatch):
    monkeypatch.setattr(
        "orcho_mcp.run_control.handoff._client_supports_form_elicitation",
        lambda _ctx: False,
    )


# ── M5: the happy path delegates to core ─────────────────────────────────────


@pytest.mark.asyncio
async def test_explicit_decision_is_recorded_by_core(run_dir) -> None:
    result = await decide_criterion_with_elicitation(
        RUN, "C3", decision="accept", note="Ran the journey.", actor="operator",
    )

    assert result.outcome == "decision_recorded"
    assert result.decision.criterion_id == "C3"
    assert result.decision.decision == "accept"
    assert result.decision.note == "Ran the journey."
    assert result.decision.actor == "operator"
    assert result.decision.run_id == RUN
    assert result.decision.decision_id

    # The wire record is the durable record — core wrote it, MCP re-read it.
    assert canonical_criterion_json(result.decision.model_dump(mode="json")) == (
        canonical_criterion_json(list_criterion_decisions(RUN, cwd=None)[-1])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "state", "blocking"),
    [("accept", "accepted", False), ("reject", "rejected", True)],
)
async def test_the_verdict_moves_the_row_and_the_summary(
    run_dir, verdict: str, state: str, blocking: bool,
) -> None:
    """M10's unit-scale sibling: the row, its proof ref, and readiness move."""
    result = await decide_criterion_with_elicitation(RUN, "C3", decision=verdict)

    assert result.matrix is not None
    row = next(r for r in result.matrix.rows if r.criterion_id == "C3")
    assert row.state == state
    assert row.blocking is blocking
    assert [ref.kind for ref in row.proof_refs] == ["human_decision"]
    assert row.proof_refs[0].id == result.decision.decision_id
    # Decided either way, the criterion is no longer waiting on a human.
    assert result.matrix.summary.pending_human_ids == []


@pytest.mark.asyncio
async def test_response_carries_the_matrix_after_the_write(run_dir) -> None:
    result = await decide_criterion_with_elicitation(RUN, "C3", decision="accept")

    assert result.matrix is not None
    assert [r.criterion_id for r in result.matrix.rows] == ["C1", "C2", "C3"]
    assert result.matrix_error is None
    assert "matrix_error" not in result.model_dump()


@pytest.mark.asyncio
async def test_a_failing_readback_does_not_lose_a_recorded_decision(
    run_dir, monkeypatch,
) -> None:
    """The write is durable, so a broken readback must not read as a failure.

    Reporting an error here would be worse than useless: the journal has
    already changed, so the operator's natural retry is refused as a
    duplicate, and they are left unable to tell a lost write from a recorded
    one. The outcome stays ``decision_recorded``; the matrix is simply absent
    with a named reason.
    """
    real = project_criterion_matrix
    calls: list[str] = []

    def _explode_after_the_write(run_id: str):
        calls.append(run_id)
        raise RuntimeError("matrix payload is unreadable")

    monkeypatch.setattr(
        "orcho_mcp.run_control.criterion_decision.project_criterion_matrix",
        _explode_after_the_write,
    )

    result = await decide_criterion_with_elicitation(RUN, "C3", decision="accept")

    assert calls == [RUN], "the readback must still be attempted"
    assert result.outcome == "decision_recorded"
    assert result.decision.decision == "accept"
    assert result.matrix is None
    assert "matrix" not in result.model_dump()
    assert result.matrix_error == "RuntimeError: matrix payload is unreadable"

    # The decision really landed — the durable journal and the real matrix
    # both show it, so the caller re-reads rather than re-deciding.
    assert [d["decision_id"] for d in list_criterion_decisions(RUN, cwd=None)] == [
        result.decision.decision_id,
    ]
    matrix = real(RUN)
    assert matrix is not None
    assert next(r for r in matrix.rows if r.criterion_id == "C3").state == "accepted"


@pytest.mark.asyncio
async def test_a_failure_before_the_write_still_raises(run_dir, monkeypatch) -> None:
    """The post-write guard must not swallow pre-write failures too.

    Same exception type, raised from the writer instead of the readback: this
    one is a genuine "nothing happened", and the caller must see it.
    """
    monkeypatch.setattr(
        "orcho_mcp.run_control.criterion_decision.record_human_decision",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("writer is down")),
    )

    with pytest.raises(RuntimeError, match="writer is down"):
        await decide_criterion_with_elicitation(RUN, "C3", decision="accept")

    assert _journal(run_dir) is None


# ── M6: elicitation and fallback express the same choices ────────────────────


@pytest.mark.asyncio
async def test_native_form_supplies_the_missing_verdict(run_dir, capable) -> None:
    ctx = _Ctx(SimpleNamespace(
        action="accept",
        data=SimpleNamespace(decision="accept", note="  looks right  "),
    ))

    result = await decide_criterion_with_elicitation(RUN, "C3", ctx=ctx)

    assert result.outcome == "decision_recorded"
    assert result.decision.decision == "accept"
    assert result.decision.note == "looks right"
    assert "C3" in ctx.prompts[0]


@pytest.mark.asyncio
async def test_explicit_note_survives_elicitation(run_dir, capable) -> None:
    """An elicited note never overwrites one the caller already supplied."""
    ctx = _Ctx(SimpleNamespace(
        action="accept",
        data=SimpleNamespace(decision="accept", note="from the form"),
    ))

    result = await decide_criterion_with_elicitation(
        RUN, "C3", note="from the caller", ctx=ctx,
    )

    assert result.decision.note == "from the caller"


@pytest.mark.asyncio
async def test_incapable_client_gets_the_same_choices_and_writes_nothing(
    run_dir, incapable,
) -> None:
    result = await decide_criterion_with_elicitation(RUN, "C3", ctx=object())

    assert result.outcome == "operator_input_required"
    assert result.run_id == RUN
    assert result.criterion_id == "C3"
    assert _journal(run_dir) is None

    # Same choices as the native form, and a ready-call carrying everything
    # except the one thing only a human can supply.
    action = result.next_actions[0]
    assert action.tool == "orcho_criterion_decide"
    assert action.kind == "operator_input_required"
    assert action.requires_operator_input is True
    assert action.choices == ["accept", "reject"]
    assert action.args == {"run_id": RUN, "criterion_id": "C3"}
    assert action.input_schema == CRITERION_DECISION_INPUT_SCHEMA
    assert action.input_schema["required"] == ["decision"]
    assert action.input_schema["properties"]["decision"]["enum"] == [
        "accept", "reject",
    ]
    # The operator is told what to actually do before deciding.
    assert result.instructions == (
        "Exercise the journey once and record accept or reject."
    )


@pytest.mark.asyncio
async def test_cancelled_form_writes_nothing_and_re_asks(run_dir, capable) -> None:
    """A declined form is the ABSENCE of a decision, never a rejection."""
    ctx = _Ctx(SimpleNamespace(action="decline", data=None))

    result = await decide_criterion_with_elicitation(RUN, "C3", ctx=ctx)

    assert result.outcome == "operator_input_required"
    assert _journal(run_dir) is None


@pytest.mark.asyncio
async def test_both_paths_produce_the_identical_core_request(
    fake_workspace, monkeypatch,
) -> None:
    """M6: the elicited verdict and the replayed one reach core the same way."""
    seen: list[dict] = []

    def _capture(run_id, **kwargs):
        seen.append({"run_id": run_id, **kwargs})
        return {
            "decision_id": "hd-C3-1", "run_id": run_id,
            "criterion_id": kwargs["criterion_id"],
            "decision": kwargs["decision"],
            "recorded_at": "2026-01-01T00:00:00Z",
        }

    monkeypatch.setattr(
        "orcho_mcp.services.criterion_projection._sdk_record_criterion_decision",
        _capture,
    )
    monkeypatch.setattr(
        "orcho_mcp.services.criterion_projection._sdk_get_criterion_matrix",
        lambda *a, **k: None,
    )

    monkeypatch.setattr(
        "orcho_mcp.run_control.handoff._client_supports_form_elicitation",
        lambda _ctx: True,
    )
    await decide_criterion_with_elicitation(
        RUN, "C3",
        ctx=_Ctx(SimpleNamespace(
            action="accept",
            data=SimpleNamespace(decision="accept", note=None),
        )),
    )
    await decide_criterion_with_elicitation(RUN, "C3", decision="accept")

    assert len(seen) == 2
    assert seen[0] == seen[1]


# ── M7: refusals leave the artifact untouched ────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("criterion_id", "why"),
    [
        ("C9", "unknown criterion"),
        ("C1", "executable criterion is not operator-decidable"),
        ("C2", "agent_assertion criterion is not operator-decidable"),
    ],
)
async def test_undecidable_criterion_fails_without_a_write(
    run_dir, criterion_id: str, why: str,
) -> None:
    with pytest.raises(InvalidPlanError):
        await decide_criterion_with_elicitation(
            RUN, criterion_id, decision="accept",
        )

    assert _journal(run_dir) is None, why


@pytest.mark.asyncio
async def test_wrong_run_fails_without_a_write(run_dir, fake_workspace) -> None:
    """A decision names a run; the criterion must belong to THAT run."""
    other = write_run(
        fake_workspace, "20260501_000002",
        meta=meta(status="done", project=str(fake_workspace), task="t"),
    )

    with pytest.raises((InvalidPlanError, Exception)):
        await decide_criterion_with_elicitation(
            "20260501_000002", "C3", decision="accept",
        )

    assert _journal(other) is None
    assert _journal(run_dir) is None


@pytest.mark.asyncio
async def test_invalid_verdict_is_refused_at_the_boundary(run_dir) -> None:
    with pytest.raises(InvalidPlanError):
        await decide_criterion_with_elicitation(RUN, "C3", decision="maybe")

    assert _journal(run_dir) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("second_verdict", ["accept", "reject"])
async def test_a_second_decision_is_refused_without_mutation(
    run_dir, second_verdict: str,
) -> None:
    """M7: a duplicate or conflicting verdict never touches the journal.

    The MCP action deliberately has no ``supersedes`` input, so it cannot
    replace a recorded head. Core rejects the append before writing; the
    journal must come out byte-identical, and the matrix must still cite the
    ORIGINAL decision. Supersession itself is core's surface (and core's
    tests) — this only pins the boundary's refusal.
    """
    first = await decide_criterion_with_elicitation(RUN, "C3", decision="accept")
    journal_before = _journal(run_dir)

    with pytest.raises(InvalidPlanError):
        await decide_criterion_with_elicitation(
            RUN, "C3", decision=second_verdict,
        )

    assert _journal(run_dir) == journal_before
    assert [d["decision_id"] for d in list_criterion_decisions(RUN, cwd=None)] == [
        first.decision.decision_id,
    ]
    matrix = project_criterion_matrix(RUN)
    assert matrix is not None
    row = next(r for r in matrix.rows if r.criterion_id == "C3")
    assert row.state == "accepted"
    assert [ref.id for ref in row.proof_refs] == [first.decision.decision_id]


@pytest.mark.asyncio
async def test_a_phase_handoff_decision_cannot_satisfy_a_criterion(run_dir) -> None:
    """M7: an unrelated operator decision is not a criterion decision."""
    handoff_dir = run_dir / "phase_handoff_decisions"
    handoff_dir.mkdir()
    (handoff_dir / "d.json").write_text(
        json.dumps({"action": "continue_with_waiver", "feedback": "ship it"}),
        encoding="utf-8",
    )

    matrix = project_criterion_matrix(RUN)

    assert matrix is not None
    row = next(r for r in matrix.rows if r.criterion_id == "C3")
    assert row.state == "pending"
    assert row.proof_refs == []


# ── F2: the durable decision on the wire ─────────────────────────────────────


@pytest.mark.asyncio
async def test_wire_decision_matches_core_with_and_without_optionals(
    run_dir,
) -> None:
    """F2: read a core-written chain through MCP and compare canonical JSON.

    The first record is written through the MCP action WITH the optional
    ``note`` / ``actor``. The replacement is written through the core SDK
    directly, because supersession is core's surface and the MCP action has no
    ``supersedes`` input — so the pair covers "optional fields present" and
    "optional fields absent" on one real journal.
    """
    first = await decide_criterion_with_elicitation(
        RUN, "C3", decision="accept", note="n", actor="a",
    )
    record_criterion_decision(
        RUN, criterion_id="C3", decision="reject",
        supersedes=first.decision.decision_id, cwd=None,
    )

    durable = list_criterion_decisions(RUN, cwd=None)
    # The MCP read path, not a second SDK call.
    via_mcp = list_human_decisions(RUN)

    assert len(via_mcp) == 2
    for wire, record in zip(via_mcp, durable, strict=True):
        assert canonical_criterion_json(wire.model_dump(mode="json")) == (
            canonical_criterion_json(record)
        )

    dumped_full, dumped_bare = (w.model_dump() for w in via_mcp)

    assert dumped_full["note"] == "n"
    assert dumped_full["actor"] == "a"
    assert "supersedes" not in dumped_full
    assert "note" not in dumped_bare
    assert "actor" not in dumped_bare
    assert dumped_bare["supersedes"] == dumped_full["decision_id"]
    assert None not in dumped_full.values()
    assert None not in dumped_bare.values()


@pytest.mark.asyncio
async def test_recorded_at_is_forwarded_verbatim(run_dir) -> None:
    result = await decide_criterion_with_elicitation(RUN, "C3", decision="accept")

    durable = list_criterion_decisions(RUN, cwd=None)[-1]

    assert result.decision.recorded_at == durable["recorded_at"]
    assert result.decision.recorded_at.endswith("Z")
