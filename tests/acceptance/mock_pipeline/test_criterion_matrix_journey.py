"""L4 journey: the criterion matrix from a real pipeline, and the human decision.

M10. Everything the matrix reports is produced by a real ``--mock`` pipeline
subprocess and read back through the public MCP tools — no hand-written
``criterion_matrix``, no monkeypatched SDK. That is the point: it proves the
plan the planner actually emitted becomes the matrix a captain actually reads.

The three-class journey (``test_three_class_journey_...``) is the load-bearing
one: a real mock run executes a declared verification gate, and the criterion
matrix built from that run reports ``proven`` for the executable criterion
*because the run produced an official receipt* — then one MCP decision flips
readiness on the same run.

Scope note, stated plainly rather than papered over. The stock mock planner
emits only ``agent_assertion`` criteria: it has no operator in the loop and
deliberately refuses to fabricate a gate identity, so it can produce neither an
``executable`` nor a ``human`` criterion. The obvious alternative — start a run
with ``from_run_plan`` pointing at a parent whose artifact carries the
three-class contract — does not help either: that child never persists a plan
artifact of its own, so it has no criterion contract at all (a core-side gap,
reported with this change). The three-class journey therefore layers an
authored plan contract onto a REAL completed run. Everything downstream stays
the run's own: the executable criterion names the gate identity read back from
that run's ``scheduled_gate_ledger.json``, its ``proven`` state comes from the
run's real disposition and its real receipt path, and the human decision is
written by core's durable writer and reduced by core's reducer. Only the
criterion text is authored.

Marked ``mcp_integration``; opt in with::

    python -m pytest -q -m mcp_integration \
        tests/acceptance/mock_pipeline/test_criterion_matrix_journey.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.mcp_integration


async def _wait_status(
    run_id: str, expected: set[str], timeout_s: float = 90.0,
) -> str:
    from orcho_mcp.tools import orcho_run_status

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = orcho_run_status(run_id)
        current = (snap.meta or {}).get("status")
        if current in expected:
            return current
        await asyncio.sleep(0.25)
    raise AssertionError(f"run {run_id} did not reach {expected!r} in {timeout_s}s")


def _pin_core_for_mock_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the detached mock pipeline import the paired core checkout."""
    from tests._core_source import pin_core_source

    core_root = pin_core_source()
    assert core_root is not None, "paired orcho-core checkout was not found"
    prior = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(p for p in (str(core_root), prior) if p),
    )


async def _finished_mock_run(mock_project: Path, task: str) -> str:
    from orcho_mcp.tools import orcho_run_start

    started = await orcho_run_start(
        task=task, project_dir=str(mock_project), mock=True, max_rounds=1,
    )
    await _wait_status(started.run_id, {"done"})
    return started.run_id


@pytest.mark.asyncio
async def test_mock_journey_reads_the_core_produced_matrix(
    mock_project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished mock run's matrix traces every criterion back to its plan."""
    from orcho_mcp.tools import orcho_run_evidence

    _pin_core_for_mock_pipeline(monkeypatch)
    run_id = await _finished_mock_run(mock_project, "criterion matrix journey")

    plan_slice = orcho_run_evidence(run_id, slice="plan").plan
    matrix = orcho_run_evidence(run_id, slice="criterion_matrix").criterion_matrix

    assert plan_slice is not None
    assert matrix is not None, (
        "a completed mock run has an accepted plan, so its criterion matrix "
        "must be present — an omitted key here would mean the durable plan "
        "artifact was never written"
    )

    # One row per plan criterion, in plan order — the traceability contract.
    plan_ids = [c.id for c in plan_slice.acceptance_criteria]
    assert plan_ids, "the mock planner emits acceptance criteria"
    assert [row.criterion_id for row in matrix.rows] == plan_ids
    assert matrix.summary.total == len(plan_ids)

    # Executors come from the plan's task ``acceptance_refs`` — MCP assigns
    # none of them itself.
    owners: dict[str, list[str]] = {}
    for task in plan_slice.task_acceptance_refs:
        for ref in task.acceptance_refs:
            owners.setdefault(ref, []).append(task.task_id)
    assert owners, "the mock plan links tasks to criteria"
    for row in matrix.rows:
        if row.criterion_id in owners:
            assert row.executors == owners[row.criterion_id]

    # An inspected criterion is advisory at most — a finished, successful run
    # does not make one ``proven``.
    for row in matrix.rows:
        if row.verify == "agent_assertion":
            assert row.method.kind == "inspection"
            assert row.state != "proven"
            assert row.blocking is False


@pytest.mark.asyncio
async def test_every_surface_agrees_on_the_mock_runs_readiness(
    mock_project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M4 end-to-end: one projection path, one verdict, four tools."""
    from orcho_mcp.tools import (
        orcho_delivery_gate,
        orcho_run_diagnose,
        orcho_run_evidence,
        orcho_run_status,
    )

    _pin_core_for_mock_pipeline(monkeypatch)
    run_id = await _finished_mock_run(mock_project, "criterion readiness parity")

    matrix = orcho_run_evidence(run_id, slice="criterion_matrix").criterion_matrix
    assert matrix is not None
    expected = matrix.summary

    assert orcho_run_status(run_id).criterion_readiness == expected
    assert orcho_run_diagnose(run_id).criterion_readiness == expected
    assert orcho_delivery_gate(run_id).criterion_readiness == expected


@pytest.mark.asyncio
async def test_a_non_human_criterion_cannot_be_decided(
    mock_project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M7 against real state: an inspected criterion is not operator-decidable."""
    from orcho_mcp.errors import InvalidPlanError
    from orcho_mcp.tools import orcho_criterion_decide, orcho_run_evidence

    _pin_core_for_mock_pipeline(monkeypatch)
    run_id = await _finished_mock_run(mock_project, "criterion decision refusal")

    matrix = orcho_run_evidence(run_id, slice="criterion_matrix").criterion_matrix
    assert matrix is not None
    target = matrix.rows[0]
    assert target.verify == "agent_assertion"

    with pytest.raises(InvalidPlanError):
        await orcho_criterion_decide(
            run_id=run_id, criterion_id=target.criterion_id, decision="accept",
        )

    run_dir = mock_project.parent / "runspace" / "runs" / run_id
    assert not (run_dir / "criterion_decisions.json").exists()
    after = orcho_run_evidence(run_id, slice="criterion_matrix").criterion_matrix
    assert after is not None
    assert after.model_dump() == matrix.model_dump()


def _write_verification_contract(project: Path) -> None:
    """Commit a one-gate scheduled contract so the run has an official gate."""
    plugin = project / ".orcho" / "multiagent" / "plugin.py"
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.write_text(
        """PLUGIN = {
    "verification": {
        "commands": {"unit": {"run": "python -c 'pass'"}},
        "schedule": [
            {"after_phase": "implement", "commands": ["unit"], "policy": "require"},
        ],
    },
}
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "declare a scheduled gate"],
        cwd=project, check=True,
    )


def _executed_gate_identity(run_dir: Path) -> dict[str, str]:
    """The identity of the gate this run actually executed, from its ledger.

    Read back rather than hardcoded: the criterion below must name the SAME
    ``(command, hook, phase)`` triple the engine recorded, so the test proves
    identity matching against real durable state instead of asserting against
    a constant that could drift from what the ledger keys.
    """
    ledger = json.loads(
        (run_dir / "scheduled_gate_ledger.json").read_text(encoding="utf-8"),
    )
    row = next(r for r in ledger["rows"] if r["gate"] == "unit")
    assert row["disposition"] == "executed_pass", (
        f"the journey needs a passing official gate; got {row['disposition']!r}"
    )
    assert row["receipt_evidence"], "a passing gate must carry a receipt"
    return {"command": row["gate"], "hook": row["hook"], "phase": row["phase"]}


def _layer_three_class_contract(run_dir: Path, gate: dict[str, str]) -> None:
    """Author a three-class criterion contract onto a real completed run.

    See the module docstring for why this is authored rather than planned. The
    rest of the plan body — tasks, summary, context — stays exactly as the
    engine wrote it, and the executable criterion points at ``gate``, the
    identity this very run executed.
    """
    path = run_dir / "parsed_plan.json"
    artifact = json.loads(path.read_text(encoding="utf-8"))
    body = artifact["plan"]
    body["acceptance_criteria"] = [
        {
            "id": "C1",
            "intent": "The declared gate proves the change",
            "verify": "executable",
            "gate_refs": [gate],
        },
        {
            "id": "C2",
            "intent": "The change reads coherently",
            "verify": "agent_assertion",
        },
        {
            "id": "C3",
            "intent": "The operator accepts the end-to-end journey",
            "verify": "human",
            "human_instructions": "Exercise the journey once and record it.",
        },
    ]
    for task in body["tasks"]:
        task["acceptance_refs"] = ["C1", "C2"]
    path.write_text(
        json.dumps({"artifact_version": artifact["artifact_version"], "plan": body}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_three_class_journey_reads_proof_decides_and_moves_readiness(
    mock_project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M10 end to end on ONE real run: read → decide → readiness moves.

    The executable row is the interesting half. It reads ``proven`` only
    because this run really executed the declared gate and really wrote a
    receipt; nothing in MCP could have produced that verdict. The human row is
    the other half: it blocks until an operator says otherwise through the
    public tool, and no amount of successful automation clears it.
    """
    from orcho_mcp.tools import orcho_criterion_decide, orcho_run_evidence

    _pin_core_for_mock_pipeline(monkeypatch)
    _write_verification_contract(mock_project)
    run_id = await _finished_mock_run(mock_project, "three-class criterion journey")

    run_dir = mock_project.parent / "runspace" / "runs" / run_id
    gate = _executed_gate_identity(run_dir)
    _layer_three_class_contract(run_dir, gate)

    before = orcho_run_evidence(run_id, slice="criterion_matrix").criterion_matrix
    assert before is not None
    rows = {row.criterion_id: row for row in before.rows}
    assert [r.verify for r in before.rows] == [
        "executable", "agent_assertion", "human",
    ]

    # Proven — and only from the run's own official receipt.
    proved = rows["C1"]
    assert proved.state == "proven"
    assert proved.blocking is False
    assert proved.method.kind == "gates"
    assert [r.model_dump() for r in proved.method.gate_refs] == [gate]
    assert [ref.kind for ref in proved.proof_refs] == ["receipt"]
    assert proved.proof_refs[0].id.startswith("verification_command_receipts/")

    # Advisory at best, never proof — even on a run that finished cleanly.
    assert rows["C2"].state != "proven"
    assert rows["C2"].blocking is False

    # The one thing a green pipeline cannot settle.
    assert rows["C3"].state == "pending"
    assert rows["C3"].blocking is True
    assert before.summary.pending_human_ids == ["C3"]
    assert before.summary.ready is False
    assert before.summary.blocking_open == 1

    # Every read surface agrees on that verdict before the decision.
    from orcho_mcp.tools import (
        orcho_delivery_gate,
        orcho_run_diagnose,
        orcho_run_status,
    )
    assert orcho_run_status(run_id).criterion_readiness == before.summary
    assert orcho_run_diagnose(run_id).criterion_readiness == before.summary
    assert orcho_delivery_gate(run_id).criterion_readiness == before.summary

    result = await orcho_criterion_decide(
        run_id=run_id,
        criterion_id="C3",
        decision="accept",
        note="Exercised the journey end to end.",
        actor="operator",
    )
    assert result.outcome == "decision_recorded"

    after = orcho_run_evidence(run_id, slice="criterion_matrix").criterion_matrix
    assert after is not None
    decided = next(r for r in after.rows if r.criterion_id == "C3")
    assert decided.state == "accepted"
    assert decided.blocking is False
    assert [ref.id for ref in decided.proof_refs] == [result.decision.decision_id]
    assert after.summary.pending_human_ids == []
    assert after.summary.blocking_open == 0
    assert after.summary.ready is True

    # …and the same four surfaces move together.
    assert orcho_run_status(run_id).criterion_readiness == after.summary
    assert orcho_run_diagnose(run_id).criterion_readiness == after.summary
    assert orcho_delivery_gate(run_id).criterion_readiness == after.summary

    # The decision is readable back through the public evidence surface.
    journal = orcho_run_evidence(
        run_id, slice="criterion_decisions",
    ).criterion_decisions
    assert journal is not None
    assert [d.decision_id for d in journal] == [result.decision.decision_id]
    assert journal[0].note == "Exercised the journey end to end."
    assert journal[0].actor == "operator"
    assert "supersedes" not in journal[0].model_dump()


@pytest.mark.asyncio
async def test_human_decision_moves_readiness_before_and_after(
    mock_project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The readiness transition a captain observes around one human decision.

    The plan declares a ``human`` criterion (see the module docstring for why
    the mock planner cannot). Everything downstream of it is real: core's
    durable writer records the decision, core's reducer rebuilds the matrix,
    and both reads go through the public MCP tool.
    """
    from orcho_mcp.tools import orcho_criterion_decide, orcho_run_evidence
    from tests.fixtures.mcp_workspace import criterion_plan, event, meta, write_run

    _pin_core_for_mock_pipeline(monkeypatch)
    run_id = "20260701_000001"
    plan = criterion_plan(
        criteria=[
            {
                "id": "C1",
                "intent": "The migration reads cleanly",
                "verify": "agent_assertion",
            },
            {
                "id": "C2",
                "intent": "The operator accepts the end-to-end journey",
                "verify": "human",
                "human_instructions": "Run the journey once and record it.",
            },
        ],
        tasks=[{"id": "T1", "goal": "migrate", "acceptance_refs": ["C1"]}],
    )
    run_dir = write_run(
        mock_project.parent, run_id,
        meta=meta(status="done", project=str(mock_project), task="human journey"),
        parsed_plan=plan,
        events=[event(1, "plan.parsed", phase="plan", payload={
            "source": "json",
            "short_summary": plan["short_summary"],
            "planning_context": plan["planning_context"],
            "subtask_count": 1,
            "has_contract": True,
            "goal": plan["goal"],
            "acceptance_criteria": plan["acceptance_criteria"],
            "subtasks": plan["tasks"],
        })],
    )

    before = orcho_run_evidence(run_id, slice="criterion_matrix").criterion_matrix
    assert before is not None
    assert before.summary.pending_human_ids == ["C2"]
    assert before.summary.ready is False
    assert next(r for r in before.rows if r.criterion_id == "C2").blocking is True

    result = await orcho_criterion_decide(
        run_id=run_id,
        criterion_id="C2",
        decision="accept",
        note="Exercised the journey end to end.",
        actor="operator",
    )
    assert result.outcome == "decision_recorded"

    after = orcho_run_evidence(run_id, slice="criterion_matrix").criterion_matrix
    assert after is not None
    row = next(r for r in after.rows if r.criterion_id == "C2")
    assert row.state == "accepted"
    assert row.blocking is False
    assert [ref.id for ref in row.proof_refs] == [result.decision.decision_id]
    assert after.summary.pending_human_ids == []
    assert after.summary.ready is True

    # The decision is durable, and its optional fields survive the round trip
    # without a ``null`` ever being written.
    journal = json.loads(
        (run_dir / "criterion_decisions.json").read_text(encoding="utf-8"),
    )
    assert "null" not in json.dumps(journal)
    assert result.decision.note == "Exercised the journey end to end."
    assert result.decision.actor == "operator"
