"""Criterion facts on the MCP read surfaces (ADR 0188) — M2 and M4.

Two claims, proved against a real synthetic run rather than a stubbed SDK:

* the ``plan`` and ``criterion_matrix`` slices carry core's typed criteria and
  matrix in a schema-valid deterministic wire shape;
* plan, evidence, status, diagnose, and delivery agree about criterion
  blockers, because they read one projection path.

The absent-vs-empty distinction gets its own tests: a legacy run must OMIT
``criterion_matrix`` (never ``null``), while a criteria-less new-format plan
must carry the explicit empty matrix.
"""
from __future__ import annotations

import dataclasses

import pytest
from sdk import (
    canonical_criterion_json,
    get_criterion_matrix,
    record_criterion_decision,
)

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.inspection.diagnosis import inspect_run_diagnosis
from orcho_mcp.inspection.evidence import inspect_run_evidence
from orcho_mcp.services.delivery_gate import project_delivery_gate
from orcho_mcp.services.run_reads import get_run_status
from tests.fixtures.mcp_workspace import criterion_plan, event, meta, write_run

RUN = "20260401_000001"


def _three_class_run(fake_workspace, run_id: str = RUN, **plan_kwargs):
    """A run carrying BOTH durable plan surfaces the read paths use.

    The criterion matrix is rebuilt from ``parsed_plan.json``; the plan slice
    is projected from the ``plan.parsed`` event. Writing only one of them
    would make a test pass for the wrong reason, so the fixture keeps them
    consistent — the same plan body in both places, as a real run has.
    """
    plan = criterion_plan(**plan_kwargs)
    return write_run(
        fake_workspace, run_id,
        meta=meta(status="done", project=str(fake_workspace), task="t"),
        parsed_plan=plan,
        events=[event(1, "plan.parsed", phase="plan", payload={
            "source": "json",
            "short_summary": plan["short_summary"],
            "planning_context": plan["planning_context"],
            "subtask_count": len(plan["tasks"]),
            "has_contract": True,
            "goal": plan["goal"],
            "acceptance_criteria": plan.get("acceptance_criteria", []),
            "subtasks": plan["tasks"],
        })],
    )


# ── M2: the matrix on the evidence surface ──────────────────────────────────


def test_matrix_slice_is_the_core_projection_verbatim(fake_workspace) -> None:
    """MCP forwards the SDK matrix byte-for-byte — no re-derivation."""
    _three_class_run(fake_workspace)

    wire = inspect_run_evidence(RUN, slice="criterion_matrix")
    core = get_criterion_matrix(RUN, cwd=None)

    assert wire.criterion_matrix is not None
    assert canonical_criterion_json(
        wire.criterion_matrix.model_dump(mode="json"),
    ) == canonical_criterion_json(core)


def test_matrix_rows_follow_plan_order_one_row_per_criterion(fake_workspace) -> None:
    _three_class_run(fake_workspace)

    matrix = inspect_run_evidence(RUN, slice="criterion_matrix").criterion_matrix

    assert matrix is not None
    assert [r.criterion_id for r in matrix.rows] == ["C1", "C2", "C3"]
    assert [r.verify for r in matrix.rows] == [
        "executable", "agent_assertion", "human",
    ]
    assert [r.method.kind for r in matrix.rows] == [
        "gates", "inspection", "manual",
    ]


def test_executable_row_without_a_receipt_is_not_proven(fake_workspace) -> None:
    """A run that never executed its gate has no proof — and says so.

    The strongest anti-fabrication check available at this layer: the plan
    NAMES the ``unit`` gate, and the row still does not read ``proven``,
    because nothing in MCP can invent a receipt for it.
    """
    _three_class_run(fake_workspace)

    matrix = inspect_run_evidence(RUN, slice="criterion_matrix").criterion_matrix

    assert matrix is not None
    row = next(r for r in matrix.rows if r.criterion_id == "C1")
    assert row.state != "proven"
    assert row.blocking is True
    assert row.proof_refs == []


def test_pending_human_criterion_blocks_readiness(fake_workspace) -> None:
    _three_class_run(fake_workspace)

    matrix = inspect_run_evidence(RUN, slice="criterion_matrix").criterion_matrix

    assert matrix is not None
    assert matrix.summary.pending_human_ids == ["C3"]
    assert matrix.summary.ready is False
    # Two blockers, named: the unproven executable criterion and the undecided
    # human one. The advisory row is not one of them.
    assert matrix.summary.blocking_open == 2
    assert [r.criterion_id for r in matrix.rows if r.blocking] == ["C1", "C3"]


def test_all_slice_includes_the_matrix(fake_workspace) -> None:
    _three_class_run(fake_workspace)

    assert inspect_run_evidence(RUN, slice="all").criterion_matrix is not None


# ── M1: typed criteria and task refs on the plan slice ───────────────────────


def test_plan_slice_carries_typed_criteria_and_task_refs(fake_workspace) -> None:
    """No lossy stringification: IDs, enums, and full gate refs survive."""
    _three_class_run(fake_workspace)

    plan = inspect_run_evidence(RUN, slice="plan").plan

    assert plan is not None
    by_id = {c.id: c for c in plan.acceptance_criteria}
    assert list(by_id) == ["C1", "C2", "C3"]

    assert by_id["C1"].verify == "executable"
    assert by_id["C1"].gate_refs is not None
    ref = by_id["C1"].gate_refs[0]
    assert (ref.command, ref.hook, ref.phase) == ("unit", "after_phase", "implement")
    assert by_id["C1"].human_instructions is None

    assert by_id["C2"].verify == "agent_assertion"
    assert by_id["C2"].gate_refs is None

    assert by_id["C3"].verify == "human"
    assert by_id["C3"].human_instructions
    assert by_id["C3"].gate_refs is None

    assert [(t.task_id, t.acceptance_refs) for t in plan.task_acceptance_refs] == [
        ("T1", ["C1", "C2"]),
    ]


def test_task_refs_keep_a_task_that_owns_no_criterion(fake_workspace) -> None:
    """The reference graph is complete: an unlinked task is present, not dropped.

    Omitting it would leave a client unable to tell "this task owns nothing"
    from "this task is missing from the projection".
    """
    _three_class_run(
        fake_workspace, "20260401_000005",
        tasks=[
            {"id": "T1", "goal": "implement", "acceptance_refs": ["C1"]},
            {"id": "T2", "goal": "document"},
        ],
    )

    plan = inspect_run_evidence("20260401_000005", slice="plan").plan

    assert plan is not None
    assert [(t.task_id, t.acceptance_refs) for t in plan.task_acceptance_refs] == [
        ("T1", ["C1"]),
        ("T2", []),
    ]


def test_task_refs_come_from_the_sdk_plan_summary_not_a_private_file(
    fake_workspace, monkeypatch,
) -> None:
    """The edges and the criteria are ONE read of ONE public projection.

    Reading the durable ``parsed_plan.json`` for the edges is exactly the
    compensation the contract forbids: two readers of one contract can
    disagree, and a private read that degrades to ``[]`` would render fully
    typed criteria beside a silently missing set of owners.

    Swapping the SDK summary is the sharpest available proof — if any part of
    the plan slice still consulted the artifact, the wire would show the
    artifact's edges instead of these.
    """
    import orcho_mcp.inspection.evidence as ev

    _three_class_run(fake_workspace)
    real = ev._sdk_get_plan_summary

    def _summary_with_marked_edges(*args, **kwargs):
        summary = real(*args, **kwargs)
        assert summary.task_acceptance_refs, (
            "the SDK plan summary must carry the per-task reference edges"
        )
        return dataclasses.replace(
            summary,
            task_acceptance_refs=(
                {"task_id": "from-the-sdk", "acceptance_refs": ["C1"]},
            ),
        )

    monkeypatch.setattr(ev, "_sdk_get_plan_summary", _summary_with_marked_edges)

    plan = inspect_run_evidence(RUN, slice="plan").plan

    assert plan is not None
    assert [(t.task_id, t.acceptance_refs) for t in plan.task_acceptance_refs] == [
        ("from-the-sdk", ["C1"]),
    ]


def test_plan_criterion_dump_omits_class_irrelevant_keys(fake_workspace) -> None:
    _three_class_run(fake_workspace)

    plan = inspect_run_evidence(RUN, slice="plan").plan
    assert plan is not None
    dumped = [c.model_dump(mode="json") for c in plan.acceptance_criteria]

    assert set(dumped[0]) == {"id", "intent", "verify", "gate_refs"}
    assert set(dumped[1]) == {"id", "intent", "verify"}
    assert set(dumped[2]) == {"id", "intent", "verify", "human_instructions"}


# ── absent vs empty ──────────────────────────────────────────────────────────


def test_run_without_a_plan_omits_the_key_never_null(fake_workspace) -> None:
    write_run(
        fake_workspace, "20260401_000002",
        meta=meta(status="done", project=str(fake_workspace), task="t"),
    )

    result = inspect_run_evidence("20260401_000002", slice="criterion_matrix")

    assert result.criterion_matrix is None
    assert "criterion_matrix" not in result.model_dump()
    assert "criterion_matrix" not in result.model_dump(mode="json")


def test_plan_with_no_criteria_is_an_explicit_empty_matrix(fake_workspace) -> None:
    """Present with ``rows: []`` — meaningfully different from absent."""
    write_run(
        fake_workspace, "20260401_000003",
        meta=meta(status="done", project=str(fake_workspace), task="t"),
        parsed_plan=criterion_plan(
            criteria=[],
            tasks=[{"id": "T1", "goal": "do the thing"}],
        ),
    )

    result = inspect_run_evidence("20260401_000003", slice="criterion_matrix")

    assert result.criterion_matrix is not None
    assert "criterion_matrix" in result.model_dump()
    assert result.criterion_matrix.rows == []
    assert result.criterion_matrix.summary.total == 0
    assert result.criterion_matrix.summary.ready is True


# ── M4: every surface agrees ─────────────────────────────────────────────────


def test_status_diagnose_delivery_and_evidence_agree(fake_workspace) -> None:
    """One projection path ⇒ one readiness verdict on four surfaces."""
    _three_class_run(fake_workspace)

    matrix = inspect_run_evidence(RUN, slice="criterion_matrix").criterion_matrix
    assert matrix is not None
    expected = matrix.summary

    assert get_run_status(RUN).criterion_readiness == expected
    assert inspect_run_diagnosis(RUN).criterion_readiness == expected
    assert project_delivery_gate(RUN).criterion_readiness == expected


def test_surfaces_omit_readiness_for_a_run_without_criteria(fake_workspace) -> None:
    write_run(
        fake_workspace, "20260401_000004",
        meta=meta(status="done", project=str(fake_workspace), task="t"),
    )

    status = get_run_status("20260401_000004")
    diagnosis = inspect_run_diagnosis("20260401_000004")
    gate = project_delivery_gate("20260401_000004")

    assert status.criterion_readiness is None
    assert diagnosis.criterion_readiness is None
    assert gate.criterion_readiness is None
    for model in (status, diagnosis, gate):
        assert "criterion_readiness" not in model.model_dump()


# ── M4: readiness and the NEXT ACTION move together ─────────────────────────


def _criterion_actions(actions) -> list[dict]:
    """The pending-criterion calls in a surface's ``next_actions``."""
    return [
        a if isinstance(a, dict) else a.model_dump()
        for a in actions
        if (a if isinstance(a, dict) else a.model_dump())["tool"]
        == "orcho_criterion_decide"
    ]


def _shipping_actions(actions) -> list[dict]:
    return [
        a if isinstance(a, dict) else a.model_dump()
        for a in actions
        if (a if isinstance(a, dict) else a.model_dump())["tool"]
        == "orcho_delivery_decide"
    ]


def test_every_surface_leads_with_the_pending_criterion_decision(
    fake_workspace,
) -> None:
    """A summary saying "not ready" must not sit beside a next action that
    ignores why.

    Reporting ``ready=False`` while the only advertised call is "continue" is
    the failure this pins: a captain forwards the ready call, core's release
    gap refuses it, and nothing in the payload explained the contradiction.
    """
    _three_class_run(fake_workspace)

    for actions in (
        get_run_status(RUN).next_actions,
        inspect_run_diagnosis(RUN).next_actions,
        project_delivery_gate(RUN).next_actions,
    ):
        decide = _criterion_actions(actions)
        assert decide, "a pending human criterion must be offered as an action"
        # It comes FIRST: it is the only call that can clear the blocker.
        first = actions[0] if isinstance(actions[0], dict) else actions[0].model_dump()
        assert first["tool"] == "orcho_criterion_decide"
        assert first["args"] == {"run_id": RUN, "criterion_id": "C3"}
        assert first["kind"] == "operator_input_required"
        assert first["requires_operator_input"] is True
        assert first["choices"] == ["accept", "reject"]
        assert first["context"]["criterion_id"] == "C3"


def test_the_criterion_action_disappears_once_the_criterion_is_decided(
    fake_workspace,
) -> None:
    """Before / after on the same run: the action tracks the readiness move."""
    _three_class_run(fake_workspace)
    assert _criterion_actions(get_run_status(RUN).next_actions)

    record_criterion_decision(
        RUN, criterion_id="C3", decision="accept", cwd=None,
        runs_dir=fake_workspace / "runspace" / "runs",
    )

    for actions in (
        get_run_status(RUN).next_actions,
        inspect_run_diagnosis(RUN).next_actions,
        project_delivery_gate(RUN).next_actions,
    ):
        assert _criterion_actions(actions) == []
    assert get_run_status(RUN).criterion_readiness.pending_human_ids == []


def test_a_run_with_no_criterion_contract_keeps_its_actions_untouched(
    fake_workspace,
) -> None:
    """The gate is additive: it must not perturb a run it does not apply to."""
    write_run(
        fake_workspace, "20260401_000006",
        meta=meta(status="done", project=str(fake_workspace), task="t"),
    )

    status = get_run_status("20260401_000006")

    assert status.criterion_readiness is None
    assert _criterion_actions(status.next_actions) == []


def test_a_blocking_criterion_demotes_a_shipping_ready_call(
    fake_workspace,
) -> None:
    """A ship call may stay visible, but never as ready-to-forward.

    Deleting it would hide a real gate from the operator; leaving it
    ``ready_call`` would invite forwarding a delivery core's release gaps will
    refuse. Demotion says the true thing — the call exists and needs an
    operator decision first.
    """
    from orcho_mcp.schemas import NextActionRecord
    from orcho_mcp.services.criterion_projection import (
        gate_actions_on_criteria,
        read_criterion_readiness,
    )

    _three_class_run(fake_workspace)
    ship = NextActionRecord(
        intent="Approve the delivery.",
        tool="orcho_delivery_decide",
        args={"run_id": RUN, "action": "approve"},
        kind="ready_call",
    )
    resume = NextActionRecord(
        intent="Resume the run.",
        tool="orcho_run_resume",
        args={"run_id": RUN},
        kind="ready_call",
    )

    gated = gate_actions_on_criteria(
        RUN, [ship, resume], read_criterion_readiness(RUN),
    )

    shipped = _shipping_actions(gated)[0]
    assert shipped["kind"] == "operator_input_required"
    assert shipped["requires_operator_input"] is True
    assert shipped["context"]["pending_human_criteria"] == ["C3"]
    assert shipped["context"]["resolve_with"] == "orcho_criterion_decide"
    # …while a non-shipping action is left exactly as it was: a criterion gate
    # is a release-boundary concern, not a reason to strand a mid-flight run.
    assert resume in gated


def test_a_non_human_blocker_still_demotes_a_shipping_ready_call(
    fake_workspace,
) -> None:
    """Accepting the human row does not clear an unproved executable row."""
    from orcho_mcp.schemas import NextActionRecord
    from orcho_mcp.services.criterion_projection import (
        gate_actions_on_criteria,
        read_criterion_readiness,
    )

    _three_class_run(fake_workspace)
    record_criterion_decision(
        RUN,
        criterion_id="C3",
        decision="accept",
        cwd=None,
        runs_dir=fake_workspace / "runspace" / "runs",
    )
    readiness = read_criterion_readiness(RUN)
    assert readiness is not None
    assert readiness.ready is False
    assert readiness.blocking_open == 1
    assert readiness.pending_human_ids == []
    assert readiness.counts_by_state["missing"] == 1

    ship = NextActionRecord(
        intent="Approve the delivery.",
        tool="orcho_delivery_decide",
        args={"run_id": RUN, "action": "approve"},
        kind="ready_call",
    )
    gated = gate_actions_on_criteria(RUN, [ship], readiness)

    assert _criterion_actions(gated) == []
    shipped = _shipping_actions(gated)[0]
    assert shipped["kind"] == "operator_input_required"
    assert shipped["context"]["criterion_blocking_open"] == 1
    assert shipped["context"]["criterion_states"]["missing"] == 1
    assert shipped["context"]["pending_human_criteria"] == []
    assert "resolve_with" not in shipped["context"]


@pytest.mark.parametrize("state", ["missing", "failed", "stale", "rejected"])
def test_every_non_pending_blocker_state_gates_shipping(state: str) -> None:
    """No blocking state needs a pending-human id to suppress shipping."""
    from orcho_mcp.schemas import NextActionRecord
    from orcho_mcp.schemas.criteria import CriterionMatrixSummaryRecord
    from orcho_mcp.services.criterion_projection import gate_actions_on_criteria

    readiness = CriterionMatrixSummaryRecord(
        total=1,
        blocking_open=1,
        ready=False,
        counts_by_state={state: 1},
        pending_human_ids=[],
    )
    ship = NextActionRecord(
        intent="Approve the delivery.",
        tool="orcho_delivery_decide",
        args={"run_id": RUN, "action": "approve"},
        kind="ready_call",
    )

    gated = gate_actions_on_criteria(RUN, [ship], readiness)

    assert _criterion_actions(gated) == []
    shipped = _shipping_actions(gated)[0]
    assert shipped["kind"] == "operator_input_required"
    assert shipped["context"]["criterion_states"] == {state: 1}


@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_all_surfaces_keep_non_human_or_rejected_blockers_closed(
    fake_workspace,
    decision: str,
) -> None:
    """A human verdict removes the prompt, not unrelated release blockers."""
    run_id = f"20260401_00001{1 if decision == 'accept' else 2}"
    _three_class_run(fake_workspace, run_id=run_id)
    record_criterion_decision(
        run_id,
        criterion_id="C3",
        decision=decision,
        cwd=None,
        runs_dir=fake_workspace / "runspace" / "runs",
    )

    surfaces = (
        get_run_status(run_id),
        inspect_run_diagnosis(run_id),
        project_delivery_gate(run_id),
    )
    expected_blockers = 1 if decision == "accept" else 2
    for surface in surfaces:
        readiness = surface.criterion_readiness
        assert readiness is not None
        assert readiness.ready is False
        assert readiness.blocking_open == expected_blockers
        assert readiness.pending_human_ids == []
        assert _criterion_actions(surface.next_actions) == []
        for shipping in _shipping_actions(surface.next_actions):
            assert shipping["kind"] == "operator_input_required"


def test_stale_criterion_sdk_fails_closed_on_every_matrix_reader(
    fake_workspace,
    monkeypatch,
) -> None:
    """Missing capability is not a legacy run whose SDK returned ``None``."""
    import orcho_mcp.services.criterion_projection as projection

    _three_class_run(fake_workspace)
    monkeypatch.setattr(projection, "_sdk_get_criterion_matrix", None)

    readers = (
        lambda: inspect_run_evidence(RUN, slice="criterion_matrix"),
        lambda: inspect_run_evidence(RUN, slice="all"),
        lambda: get_run_status(RUN),
        lambda: inspect_run_diagnosis(RUN),
        lambda: project_delivery_gate(RUN),
    )
    for reader in readers:
        with pytest.raises(InvalidPlanError, match="criterion_matrix unavailable"):
            reader()


def test_stale_criterion_sdk_fails_closed_on_decision_log_read(
    fake_workspace,
    monkeypatch,
) -> None:
    import orcho_mcp.services.criterion_projection as projection

    _three_class_run(fake_workspace)
    monkeypatch.setattr(projection, "_sdk_list_criterion_decisions", None)

    for slice_name in ("criterion_decisions", "all"):
        with pytest.raises(
            InvalidPlanError,
            match="criterion_decisions unavailable",
        ):
            inspect_run_evidence(RUN, slice=slice_name)


def test_malformed_matrix_fails_closed_on_every_read_surface(
    fake_workspace,
    monkeypatch,
) -> None:
    """Invalid current evidence never degrades to omitted readiness."""
    import orcho_mcp.services.criterion_projection as projection

    _three_class_run(fake_workspace)
    monkeypatch.setattr(
        projection,
        "_sdk_get_criterion_matrix",
        lambda *_, **__: {
            "rows": [],
            "summary": {
                "total": 1,
                "blocking_open": 1,
                "ready": True,
                "counts_by_state": {"missing": 1},
                "pending_human_ids": [],
            },
        },
    )

    readers = (
        lambda: inspect_run_evidence(RUN, slice="criterion_matrix"),
        lambda: inspect_run_evidence(RUN, slice="all"),
        lambda: get_run_status(RUN),
        lambda: inspect_run_diagnosis(RUN),
        lambda: project_delivery_gate(RUN),
    )
    for reader in readers:
        with pytest.raises(InvalidPlanError, match="contradicts blocking_open"):
            reader()
