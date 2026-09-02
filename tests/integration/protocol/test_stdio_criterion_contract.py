"""L3 stdio: a client reads the three criterion classes and resolves a human one.

M8-M9. The in-process tests prove the projection; this proves an actual MCP
client can *see* and *use* it — the tool is registered, its JSON Schema
generates, the matrix survives JSON-RPC framing, and the decision round-trips
without polluting stdout.

Two shapes are deliberately checked at the wire, not just in Python: the
``criterion_matrix`` key is ABSENT for a run with no criterion contract (never
``null``), and the optional human-decision keys are absent when unused.
"""
from __future__ import annotations

import pytest

pytest.importorskip("mcp.client.stdio")

from tests.fixtures.mcp_workspace import (  # noqa: E402
    criterion_plan,
    event,
    meta,
    write_run,
)
from tests.fixtures.stdio import initialized_stdio_session  # noqa: E402

RUN = "20260601_000001"
BARE_RUN = "20260601_000002"


def _seed(fake_workspace) -> None:
    plan = criterion_plan()
    write_run(
        fake_workspace, RUN,
        meta=meta(status="done", project=str(fake_workspace), task="criterion"),
        parsed_plan=plan,
        events=[event(1, "plan.parsed", phase="plan", payload={
            "source": "json",
            "short_summary": plan["short_summary"],
            "planning_context": plan["planning_context"],
            "subtask_count": len(plan["tasks"]),
            "has_contract": True,
            "goal": plan["goal"],
            "acceptance_criteria": plan["acceptance_criteria"],
            "subtasks": plan["tasks"],
        })],
    )
    write_run(
        fake_workspace, BARE_RUN,
        meta=meta(status="done", project=str(fake_workspace), task="legacy"),
    )


@pytest.mark.anyio
async def test_stdio_client_reads_and_resolves_the_criterion_contract(
    fake_workspace,
) -> None:
    _seed(fake_workspace)

    async with initialized_stdio_session(fake_workspace) as (session, _):
        tools = {t.name for t in (await session.list_tools()).tools}
        assert "orcho_criterion_decide" in tools

        matrix_result = await session.call_tool(
            "orcho_run_evidence",
            {"run_id": RUN, "slice": "criterion_matrix"},
        )
        assert matrix_result.isError is False
        before = matrix_result.structuredContent["criterion_matrix"]

        # All three classes are visible, each with the shape its class admits.
        assert [r["criterion_id"] for r in before["rows"]] == ["C1", "C2", "C3"]
        methods = {r["criterion_id"]: r["method"] for r in before["rows"]}
        assert set(methods["C1"]) == {"kind", "gate_refs"}
        assert methods["C1"]["gate_refs"][0] == {
            "command": "unit", "hook": "after_phase", "phase": "implement",
        }
        assert set(methods["C2"]) == {"kind"}
        assert set(methods["C3"]) == {"kind", "instructions"}
        assert before["summary"]["pending_human_ids"] == ["C3"]
        assert before["summary"]["ready"] is False

        # The plan slice carries the same criteria typed, plus the task refs.
        plan_result = await session.call_tool(
            "orcho_run_evidence", {"run_id": RUN, "slice": "plan"},
        )
        plan = plan_result.structuredContent["plan"]
        assert [c["id"] for c in plan["acceptance_criteria"]] == ["C1", "C2", "C3"]
        assert plan["task_acceptance_refs"] == [
            {"task_id": "T1", "acceptance_refs": ["C1", "C2"]},
        ]

        # Resolve the human criterion with an explicit verdict.
        decided = await session.call_tool(
            "orcho_criterion_decide",
            {"run_id": RUN, "criterion_id": "C3", "decision": "accept"},
        )
        assert decided.isError is False
        # A union-returning tool is wrapped under ``result`` by FastMCP.
        payload = decided.structuredContent["result"]
        assert payload["outcome"] == "decision_recorded"
        decision = payload["decision"]
        assert decision["decision"] == "accept"
        assert decision["recorded_at"].endswith("Z")
        # Unused optional keys are absent on the wire, never ``null``.
        assert "note" not in decision
        assert "actor" not in decision
        assert "supersedes" not in decision

        # …and the readiness transition is observable from the read surface.
        after = (await session.call_tool(
            "orcho_run_evidence",
            {"run_id": RUN, "slice": "criterion_matrix"},
        )).structuredContent["criterion_matrix"]
        row = next(r for r in after["rows"] if r["criterion_id"] == "C3")
        assert row["state"] == "accepted"
        assert row["proof_refs"] == [
            {"kind": "human_decision", "id": decision["decision_id"]},
        ]
        assert after["summary"]["pending_human_ids"] == []


@pytest.mark.anyio
async def test_stdio_run_without_criteria_omits_the_key(fake_workspace) -> None:
    """Absent is an omitted key on the wire — a client must not see ``null``."""
    _seed(fake_workspace)

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_run_evidence",
            {"run_id": BARE_RUN, "slice": "criterion_matrix"},
        )

    assert result.isError is False
    assert "criterion_matrix" not in result.structuredContent


@pytest.mark.anyio
async def test_stdio_decision_without_a_verdict_writes_nothing(
    fake_workspace,
) -> None:
    """A plain client gets the typed missing-input record, not a guess.

    ``initialized_stdio_session`` advertises no elicitation capability, so
    this is the fallback path — and it must reach the handler through the
    generated input schema, proving ``decision`` really is optional there.
    """
    _seed(fake_workspace)

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_criterion_decide",
            {"run_id": RUN, "criterion_id": "C3"},
        )
        assert result.isError is False
        payload = result.structuredContent["result"]
        assert payload["outcome"] == "operator_input_required"
        action = payload["next_actions"][0]
        assert action["choices"] == ["accept", "reject"]
        assert action["requires_operator_input"] is True
        assert action["args"] == {"run_id": RUN, "criterion_id": "C3"}

        # Nothing was recorded: the criterion is still pending.
        matrix = (await session.call_tool(
            "orcho_run_evidence",
            {"run_id": RUN, "slice": "criterion_matrix"},
        )).structuredContent["criterion_matrix"]

    row = next(r for r in matrix["rows"] if r["criterion_id"] == "C3")
    assert row["state"] == "pending"
    assert matrix["summary"]["pending_human_ids"] == ["C3"]


@pytest.mark.anyio
async def test_stdio_client_reads_the_durable_decision_log(fake_workspace) -> None:
    """F2 round-trip: core writes the chain, a real client reads it back.

    The decision a matrix row cites by id has to be *readable* — a client that
    reconnects after a resume needs the verdict, its timestamp, and its audit
    fields, not just a proof reference pointing at them. This drives that
    across the real JSON-RPC boundary and compares canonical JSON against the
    SDK, so the wire cannot quietly reshape a durable record.

    The chain deliberately mixes both shapes: the first decision carries
    ``note`` + ``actor``, the replacement carries only ``supersedes``.
    """
    from sdk import (
        canonical_criterion_json,
        list_criterion_decisions,
        record_criterion_decision,
    )

    _seed(fake_workspace)

    first = record_criterion_decision(
        RUN, criterion_id="C3", decision="reject",
        note="The empty state is unreadable.", actor="operator",
        cwd=None, runs_dir=fake_workspace / "runspace" / "runs",
    )
    record_criterion_decision(
        RUN, criterion_id="C3", decision="accept",
        supersedes=first["decision_id"],
        cwd=None, runs_dir=fake_workspace / "runspace" / "runs",
    )
    durable = list_criterion_decisions(
        RUN, cwd=None, runs_dir=fake_workspace / "runspace" / "runs",
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_run_evidence",
            {"run_id": RUN, "slice": "criterion_decisions"},
        )
        assert result.isError is False
        over_the_wire = result.structuredContent["criterion_decisions"]

        # The matrix cites the chain HEAD, and the client can now resolve it.
        matrix = (await session.call_tool(
            "orcho_run_evidence",
            {"run_id": RUN, "slice": "criterion_matrix"},
        )).structuredContent["criterion_matrix"]

    assert len(over_the_wire) == 2
    for wire, record in zip(over_the_wire, durable, strict=True):
        assert canonical_criterion_json(wire) == canonical_criterion_json(record)

    with_optionals, replacement = over_the_wire
    assert with_optionals["note"] == "The empty state is unreadable."
    assert with_optionals["actor"] == "operator"
    assert "supersedes" not in with_optionals
    assert "note" not in replacement
    assert "actor" not in replacement
    assert replacement["supersedes"] == with_optionals["decision_id"]
    assert replacement["recorded_at"].endswith("Z")

    row = next(r for r in matrix["rows"] if r["criterion_id"] == "C3")
    assert row["state"] == "accepted"
    assert row["proof_refs"] == [
        {"kind": "human_decision", "id": replacement["decision_id"]},
    ]


@pytest.mark.anyio
async def test_stdio_decision_log_is_empty_for_an_undecided_run(
    fake_workspace,
) -> None:
    """An empty log is ``[]`` — a readable fact, not an error or a null."""
    _seed(fake_workspace)

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_run_evidence",
            {"run_id": RUN, "slice": "criterion_decisions"},
        )

    assert result.isError is False
    assert result.structuredContent["criterion_decisions"] == []
