"""Unit tests for ``orcho_run_evidence(slice="verification_cockpit")``.

Pins the MCP cockpit projection — the typed, actionable view derived from the
SAME ``sdk.get_verification_timeline`` projection that feeds the
``verification_timeline`` slice. The SDK source of truth is monkeypatched at the
evidence module's own seam (the cockpit performs no SDK call of its own), so
these tests cover only the derived cockpit mapping:

  - all six gate statuses project into rows with a legal status (never MANUAL);
  - deterministic ``trigger`` derivation (auto / manual / operator_only) and the
    precedence of operator_only over auto;
  - manual-only gates are present but never read as a residual automation
    failure;
  - missing/stale/failed required gates carry rerun_hint + evidence, present /
    manual gates do not;
  - inherited gates thread source_run_id;
  - the header (has_contract / mode / envs / policy_summary / effect) for the
    with-contract and no-contract cases;
  - slice isolation, slice="all" carrying both cockpit + timeline, and the
    stale-core capability precondition.
"""
from __future__ import annotations

import pytest

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.inspection.evidence import inspect_run_evidence

_SEAM = "orcho_mcp.inspection.evidence._sdk_get_verification_timeline"

_LEGAL_STATUSES = {"PASS", "FAIL", "MISSING", "STALE", "SKIPPED", "FRESH"}


def _gate(**kw):
    """Build an SDK GateProjection with sensible defaults."""
    from sdk import GateProjection

    base = dict(
        command="lint",
        env="ci",
        hook="before_delivery",
        policy="require",
        required=True,
        status="PASS",
        receipt_path="",
        source_run_id="",
        inherited=False,
        stale_reason="",
        searched_run_dirs=(),
        rerun_hint=(),
    )
    base.update(kw)
    return GateProjection(**base)


def _autorun(**kw):
    """Build an SDK AutorunEvent with defaults."""
    from sdk import AutorunEvent

    base = dict(
        phase="final_acceptance",
        source="stage9_autorun",
        hook_label="pre-final auto-run",
        ran_pass=(),
        ran_fail=(),
        skipped_fresh=(),
        skipped_manual=(),
        receipt_paths=(),
    )
    base.update(kw)
    return AutorunEvent(**base)


def _projection(**kw):
    """Build an SDK VerificationTimelineProjection with defaults."""
    from sdk import VerificationTimelineProjection

    base = dict(run_id="rid", project="/proj", has_contract=True)
    base.update(kw)
    return VerificationTimelineProjection(**base)


def _patch(monkeypatch, projection):
    monkeypatch.setattr(_SEAM, lambda *, run_id, **_: projection)


def _cockpit(monkeypatch, projection):
    _patch(monkeypatch, projection)
    return inspect_run_evidence("rid", slice="verification_cockpit").verification_cockpit


# ── (1) all six statuses project, every row carries a legal status ───────────


def test_all_six_statuses_project_into_legal_rows(monkeypatch) -> None:
    proj = _projection(
        gates=(
            _gate(command="lint", status="PASS"),
            _gate(command="fmt", status="FRESH"),
            _gate(
                command="unit", status="MISSING",
                searched_run_dirs=("/runs/rid",),
                rerun_hint=("orcho verify run --required --run-id rid",),
            ),
            _gate(
                command="smoke", status="FAIL",
                receipt_path="/runs/rid/verification_command_receipts/smoke.json",
                rerun_hint=("orcho verify run smoke --run-id rid",),
            ),
            _gate(
                command="e2e", status="STALE",
                stale_reason="checkout HEAD moved a -> b",
                rerun_hint=("orcho verify run e2e --run-id rid",),
            ),
            _gate(command="manual_gate", status="SKIPPED", policy="manual_only"),
        ),
        residual_missing=("unit",),
        residual_failed=("smoke",),
        residual_stale=("e2e",),
        manual_only=("manual_gate",),
    )
    ck = _cockpit(monkeypatch, proj)

    assert ck is not None
    assert {g.command: g.status for g in ck.gates} == {
        "lint": "PASS", "fmt": "FRESH", "unit": "MISSING",
        "smoke": "FAIL", "e2e": "STALE", "manual_gate": "SKIPPED",
    }
    # Every row's status is one of the six legal values — never MANUAL.
    assert all(g.status in _LEGAL_STATUSES for g in ck.gates)
    assert all(g.status != "MANUAL" for g in ck.gates)


# ── (2) trigger derivation + operator_only precedence over auto ──────────────


def test_trigger_auto_manual_operator_only(monkeypatch) -> None:
    proj = _projection(
        gates=(
            # In autorun ran_pass -> auto.
            _gate(command="unit", status="PASS"),
            # Plain delivery gate, not auto-run -> manual.
            _gate(command="lint", status="MISSING", policy="warn"),
            # manual_only set membership -> operator_only.
            _gate(command="manual_gate", status="SKIPPED", policy="manual_only"),
        ),
        manual_only=("manual_gate",),
        autorun_events=(_autorun(ran_pass=("unit",)),),
    )
    ck = _cockpit(monkeypatch, proj)
    trig = {g.command: g.trigger for g in ck.gates}

    assert trig == {
        "unit": "auto", "lint": "manual", "manual_gate": "operator_only",
    }


def test_operator_only_takes_precedence_over_auto(monkeypatch) -> None:
    # A command both in manual_only AND in an autorun ran_pass must resolve to
    # operator_only — the manual classification wins over the auto signal.
    proj = _projection(
        gates=(_gate(command="e2e", status="SKIPPED", policy="manual_only"),),
        manual_only=("e2e",),
        autorun_events=(_autorun(ran_pass=("e2e",)),),
    )
    ck = _cockpit(monkeypatch, proj)
    assert ck.gates[0].trigger == "operator_only"

    # The policy='manual_only' path alone (no manual_only membership) also wins.
    proj2 = _projection(
        gates=(_gate(command="e2e", status="SKIPPED", policy="manual_only"),),
        autorun_events=(_autorun(ran_pass=("e2e",)),),
    )
    ck2 = _cockpit(monkeypatch, proj2)
    assert ck2.gates[0].trigger == "operator_only"


# ── (3) manual-only gate present but not a residual automation failure ───────


def test_manual_only_gate_is_not_a_residual_failure(monkeypatch) -> None:
    proj = _projection(
        gates=(
            _gate(command="lint", status="PASS"),
            # Reproduce the real SDK shape: a manual_only command that is a
            # member of contract.required carries required=True from the SDK.
            _gate(
                command="manual_gate", status="SKIPPED",
                policy="manual_only", required=True,
            ),
        ),
        manual_only=("manual_gate",),
    )
    ck = _cockpit(monkeypatch, proj)
    by_cmd = {g.command: g for g in ck.gates}

    manual = by_cmd["manual_gate"]
    assert manual.status == "SKIPPED"
    assert manual.policy == "manual_only"
    assert manual.trigger == "operator_only"
    # Even though the SDK gate reports required=True (contract.required
    # membership), the cockpit row derives required from the EFFECTIVE policy,
    # so a manual_only gate is NOT a blocking required gate (requirement F1).
    assert manual.required is False
    # Present in the cockpit gates, listed under manual_only, and NOT treated as
    # a residual missing/failed automation failure.
    assert "manual_gate" in ck.manual_only
    assert "manual_gate" not in ck.residual_missing
    assert "manual_gate" not in ck.residual_failed
    assert "manual_gate" not in ck.residual_stale


# ── (4) rerun_hint + evidence on missing/stale/failed; empty on present ──────


def test_required_failures_carry_hint_and_evidence(monkeypatch) -> None:
    proj = _projection(
        gates=(
            _gate(command="lint", status="PASS"),
            _gate(
                command="unit", status="MISSING",
                searched_run_dirs=("/runs/rid",),
                rerun_hint=("orcho verify run --required --run-id rid",),
            ),
            _gate(
                command="smoke", status="FAIL",
                receipt_path="/runs/rid/verification_command_receipts/smoke.json",
                rerun_hint=("orcho verify run smoke --run-id rid",),
            ),
            _gate(
                command="e2e", status="STALE",
                stale_reason="checkout HEAD moved a -> b",
                rerun_hint=("orcho verify run e2e --run-id rid",),
            ),
        ),
        residual_missing=("unit",),
        residual_failed=("smoke",),
        residual_stale=("e2e",),
    )
    ck = _cockpit(monkeypatch, proj)
    by_cmd = {g.command: g for g in ck.gates}

    assert by_cmd["unit"].rerun_hint
    assert by_cmd["smoke"].rerun_hint
    assert by_cmd["smoke"].receipt_path == (
        "/runs/rid/verification_command_receipts/smoke.json"
    )
    assert by_cmd["e2e"].rerun_hint
    assert by_cmd["e2e"].stale_reason == "checkout HEAD moved a -> b"

    # The present gate carries no rerun_hint.
    assert by_cmd["lint"].rerun_hint == []


# ── (5) inherited gate threads source_run_id ─────────────────────────────────


def test_inherited_gate_threads_source_run(monkeypatch) -> None:
    proj = _projection(
        gates=(
            _gate(
                command="unit", status="PASS",
                receipt_path="/runs/parent/verification_command_receipts/unit.json",
                source_run_id="parent_run",
                inherited=True,
            ),
        ),
        inherited=("unit from run parent_run",),
    )
    ck = _cockpit(monkeypatch, proj)
    unit = ck.gates[0]

    assert unit.inherited is True
    assert unit.source_run_id == "parent_run"
    assert unit.receipt_path == (
        "/runs/parent/verification_command_receipts/unit.json"
    )
    assert "unit from run parent_run" in ck.inherited


# ── (6) FRESH via autorun skipped_fresh -> trigger auto ──────────────────────


def test_fresh_via_skipped_fresh_is_auto(monkeypatch) -> None:
    proj = _projection(
        gates=(_gate(command="fmt", status="FRESH"),),
        autorun_events=(_autorun(skipped_fresh=("fmt",)),),
    )
    ck = _cockpit(monkeypatch, proj)
    fmt = ck.gates[0]
    assert fmt.status == "FRESH"
    assert fmt.trigger == "auto"


# ── (7) header with a contract: has_contract True, envs, policy_summary ──────


def test_header_with_contract_require(monkeypatch) -> None:
    proj = _projection(
        gates=(
            _gate(command="lint", status="PASS", policy="require"),
            _gate(command="warn_cmd", status="MISSING", policy="warn"),
        ),
        env_statuses=(("ci", True), ("e2e_env", False)),
        residual_missing=("warn_cmd",),
    )
    ck = _cockpit(monkeypatch, proj)

    assert ck.has_contract is True
    assert ck.mode is None
    assert ck.envs == ["ci", "e2e_env"]
    # require dominates the policy aggregate.
    assert ck.policy_summary == "require"
    assert ck.effect == "blocks delivery on missing/failed receipts"


def test_header_with_contract_warn_only(monkeypatch) -> None:
    proj = _projection(
        gates=(
            _gate(command="lint", status="MISSING", policy="warn"),
            _gate(command="fmt", status="MISSING", policy="warn"),
        ),
        env_statuses=(("ci", True),),
        residual_missing=("lint", "fmt"),
    )
    ck = _cockpit(monkeypatch, proj)

    assert ck.has_contract is True
    assert ck.policy_summary == "warn"
    assert ck.effect == "warn on missing/failed receipts"


# ── (8) header without a contract: has_contract False, empty, no gates ───────


def test_header_without_contract(monkeypatch) -> None:
    proj = _projection(has_contract=False, gates=())
    ck = _cockpit(monkeypatch, proj)

    assert ck is not None
    assert ck.has_contract is False
    assert ck.gates == []
    assert ck.policy_summary == "none"
    assert ck.effect == "no verification gates"


# ── (8a) required reflects effective policy, not raw contract membership ──────


def test_required_reflects_effective_policy(monkeypatch) -> None:
    # All three gates carry the SDK's required=True (contract.required
    # membership). Only the gate whose effective policy is 'require' is a
    # genuinely blocking required gate in the cockpit (requirement F1).
    proj = _projection(
        gates=(
            _gate(command="blocking", status="MISSING", policy="require",
                  required=True),
            _gate(command="warned", status="MISSING", policy="warn",
                  required=True),
            _gate(command="op", status="SKIPPED", policy="manual_only",
                  required=True),
        ),
        manual_only=("op",),
        residual_missing=("blocking", "warned"),
    )
    ck = _cockpit(monkeypatch, proj)
    req = {g.command: g.required for g in ck.gates}

    assert req == {"blocking": True, "warned": False, "op": False}


# ── (8b) a manual-only-only contract still reports gates, never 'none' ───────


def test_manual_only_only_contract_is_not_none(monkeypatch) -> None:
    # A contract whose every gate is manual_only is still a contract with
    # gates: the header must not claim "no verification gates" while listing
    # gate rows (requirement F2).
    proj = _projection(
        has_contract=True,
        gates=(
            _gate(command="e2e", status="SKIPPED", policy="manual_only",
                  required=True),
            _gate(command="broad", status="SKIPPED", policy="manual_only",
                  required=False),
        ),
        manual_only=("e2e", "broad"),
    )
    ck = _cockpit(monkeypatch, proj)

    assert ck.has_contract is True
    assert len(ck.gates) == 2
    # Not 'none' — gates exist; folds to 'suggest' deterministically.
    assert ck.policy_summary == "suggest"
    assert ck.effect == "suggests rerun on missing/failed receipts"
    assert ck.effect != "no verification gates"


# ── (9) slice isolation + slice="all" carries both cockpit and timeline ──────


def test_slice_isolates_other_slices(monkeypatch) -> None:
    proj = _projection(gates=(_gate(command="lint", status="PASS"),))
    _patch(monkeypatch, proj)

    result = inspect_run_evidence("rid", slice="verification_cockpit")
    assert result.verification_cockpit is not None
    assert result.verification_timeline is None
    assert result.plan is None
    assert result.findings is None
    assert result.verification_receipts is None


def test_all_slice_includes_cockpit_and_timeline(monkeypatch) -> None:
    proj = _projection(gates=(_gate(command="lint", status="PASS"),))
    _patch(monkeypatch, proj)
    import orcho_mcp.inspection.evidence as ev

    monkeypatch.setattr(ev, "_sdk_get_plan_summary", lambda *a, **k: _PLAN_STUB())
    monkeypatch.setattr(ev, "_sdk_list_findings", lambda *a, **k: [])
    monkeypatch.setattr(ev, "_sdk_list_evidence_commands", lambda *a, **k: [])
    monkeypatch.setattr(ev, "_sdk_list_evidence_artifacts", lambda *a, **k: [])
    monkeypatch.setattr(ev, "_sdk_get_errors_halt", lambda *a, **k: _ERRORS_STUB())
    monkeypatch.setattr(ev, "_sdk_list_sub_runs", lambda *a, **k: [])
    monkeypatch.setattr(ev, "_sdk_list_subtask_receipts", lambda *a, **k: [])
    monkeypatch.setattr(ev, "_read_verification_receipts", lambda run_dir: [])
    monkeypatch.setattr(ev, "_sdk_list_handoff_advice", lambda *a, **k: None)
    monkeypatch.setattr(
        ev, "find_run_dir", lambda run_id: __import__("pathlib").Path("/tmp"),
    )

    result = inspect_run_evidence("rid", slice="all")
    assert result.verification_cockpit is not None
    assert result.verification_timeline is not None
    assert result.verification_cockpit.gates[0].command == "lint"


def test_all_slice_reads_sdk_projection_once(monkeypatch) -> None:
    # The cockpit and timeline are built from ONE shared SDK read — slice="all"
    # must not call get_verification_timeline twice.
    proj = _projection(gates=(_gate(command="lint", status="PASS"),))
    calls = {"n": 0}

    def _counting(*, run_id, **_):
        calls["n"] += 1
        return proj

    monkeypatch.setattr(_SEAM, _counting)
    import orcho_mcp.inspection.evidence as ev

    monkeypatch.setattr(ev, "_sdk_get_plan_summary", lambda *a, **k: _PLAN_STUB())
    monkeypatch.setattr(ev, "_sdk_list_findings", lambda *a, **k: [])
    monkeypatch.setattr(ev, "_sdk_list_evidence_commands", lambda *a, **k: [])
    monkeypatch.setattr(ev, "_sdk_list_evidence_artifacts", lambda *a, **k: [])
    monkeypatch.setattr(ev, "_sdk_get_errors_halt", lambda *a, **k: _ERRORS_STUB())
    monkeypatch.setattr(ev, "_sdk_list_sub_runs", lambda *a, **k: [])
    monkeypatch.setattr(ev, "_sdk_list_subtask_receipts", lambda *a, **k: [])
    monkeypatch.setattr(ev, "_read_verification_receipts", lambda run_dir: [])
    monkeypatch.setattr(ev, "_sdk_list_handoff_advice", lambda *a, **k: None)
    monkeypatch.setattr(
        ev, "find_run_dir", lambda run_id: __import__("pathlib").Path("/tmp"),
    )

    inspect_run_evidence("rid", slice="all")
    assert calls["n"] == 1


# ── (10) stale-core capability precondition for cockpit + all ────────────────


def test_stale_core_fails_loud_on_explicit_cockpit_slice(monkeypatch) -> None:
    monkeypatch.setattr(_SEAM, None)
    with pytest.raises(InvalidPlanError) as exc:
        inspect_run_evidence("rid", slice="verification_cockpit")
    assert "get_verification_timeline" in str(exc.value)


def test_stale_core_fails_loud_on_all_slice(monkeypatch) -> None:
    monkeypatch.setattr(_SEAM, None)
    with pytest.raises(InvalidPlanError) as exc:
        inspect_run_evidence("rid", slice="all")
    assert "get_verification_timeline" in str(exc.value)


def _PLAN_STUB():
    from sdk import PlanSummary

    return PlanSummary(
        source="plan", short_summary="", planning_context="",
        subtask_count=0, has_contract=False, goal="",
        acceptance_criteria=(), owned_files=(), commands_to_run=(),
        risks=(), review_focus=(),
    )


def _ERRORS_STUB():
    from sdk import ErrorsAndHalt

    return ErrorsAndHalt(
        status="done", errors=(), halt_reason=None, halted_at=None,
        error_summary=None,
    )
