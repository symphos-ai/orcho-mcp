"""Unit tests for ``orcho_run_evidence(slice="verification_timeline")``.

Pins the MCP projection layer for the official verification-gate timeline.
The SDK source of truth (``sdk.get_verification_timeline``, tested in
orcho-core) is monkeypatched at this module's own seam, so these tests cover
only the wire-record mapping: the six-value status enum (no ``MANUAL``),
manual-only as ``SKIPPED`` + ``policy='manual_only'``, per-gate
``rerun_hint`` / ``searched_run_dirs`` binding, the present/missing/stale/
failed/inherited/fresh cases, the empty (no-contract) case, slice isolation,
and the invalid-slice / unknown-run error mapping.
"""
from __future__ import annotations

import pytest

from orcho_mcp.errors import InvalidPlanError, RunNotFoundError
from orcho_mcp.inspection.evidence import inspect_run_evidence

_SEAM = "orcho_mcp.inspection.evidence._sdk_get_verification_timeline"


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


def _projection(**kw):
    """Build an SDK VerificationTimelineProjection with defaults."""
    from sdk import VerificationTimelineProjection

    base = dict(run_id="rid", project="/proj", has_contract=True)
    base.update(kw)
    return VerificationTimelineProjection(**base)


def _patch(monkeypatch, projection):
    monkeypatch.setattr(_SEAM, lambda *, run_id, **_: projection)


def test_all_six_statuses_project_without_manual(monkeypatch) -> None:
    # present->PASS, fresh->FRESH, missing->MISSING, failed->FAIL,
    # stale->STALE, manual->SKIPPED. No gate carries the (illegal) MANUAL.
    proj = _projection(
        gates=(
            _gate(command="lint", status="PASS"),
            _gate(command="fmt", status="FRESH"),
            _gate(
                command="unit", status="MISSING",
                searched_run_dirs=("/runs/rid",),
                rerun_hint=("orcho verify run --required --run-id rid --project /proj",),
            ),
            _gate(
                command="smoke", status="FAIL",
                receipt_path="/runs/rid/verification_command_receipts/smoke.json",
                searched_run_dirs=("/runs/rid",),
                rerun_hint=("orcho verify run smoke --run-id rid --project /proj",),
            ),
            _gate(
                command="e2e", status="STALE", stale_reason="checkout HEAD moved a -> b",
                searched_run_dirs=("/runs/rid",),
                rerun_hint=("orcho verify run e2e --run-id rid --project /proj",),
            ),
            _gate(
                command="manual_gate", status="SKIPPED", policy="manual_only",
            ),
        ),
        residual_missing=("unit",),
        residual_failed=("smoke",),
        residual_stale=("e2e",),
        manual_only=("manual_gate",),
    )
    _patch(monkeypatch, proj)

    result = inspect_run_evidence("rid", slice="verification_timeline")

    assert result.slice == "verification_timeline"
    tl = result.verification_timeline
    assert tl is not None
    assert tl.has_contract is True
    by_cmd = {g.command: g for g in tl.gates}

    assert {g.command: g.status for g in tl.gates} == {
        "lint": "PASS", "fmt": "FRESH", "unit": "MISSING",
        "smoke": "FAIL", "e2e": "STALE", "manual_gate": "SKIPPED",
    }
    # No gate carries a MANUAL status (it is not even a legal enum value).
    assert all(g.status != "MANUAL" for g in tl.gates)

    # Manual-only gate: SKIPPED + policy manual_only + in the manual_only set,
    # and it carries NO rerun_hint / searched_run_dirs.
    manual = by_cmd["manual_gate"]
    assert manual.status == "SKIPPED"
    assert manual.policy == "manual_only"
    assert "manual_gate" in tl.manual_only
    assert manual.rerun_hint == []
    assert manual.searched_run_dirs == []


def test_per_gate_hint_and_searched_dirs_bound_to_missing_required_gate(
    monkeypatch,
) -> None:
    proj = _projection(
        gates=(
            _gate(command="lint", status="PASS"),
            _gate(
                command="unit", status="MISSING",
                searched_run_dirs=("/runs/rid",),
                rerun_hint=(
                    "orcho verify env --env ci --run-id rid --project /proj",
                    "orcho verify run --required --run-id rid --project /proj",
                ),
            ),
        ),
        residual_missing=("unit",),
        searched_run_dirs=("/runs/rid",),
        suggested_commands=(
            "orcho verify env --env ci --run-id rid --project /proj",
            "orcho verify run --required --run-id rid --project /proj",
        ),
    )
    _patch(monkeypatch, proj)

    tl = inspect_run_evidence("rid", slice="verification_timeline").verification_timeline
    by_cmd = {g.command: g for g in tl.gates}

    # The missing required gate carries its own non-empty hint + searched dirs,
    # bound to THAT gate (not just the aggregate).
    unit = by_cmd["unit"]
    assert unit.status == "MISSING"
    assert unit.searched_run_dirs == ["/runs/rid"]
    assert unit.rerun_hint
    assert set(unit.rerun_hint).issubset(set(tl.suggested_commands))

    # The present gate carries neither.
    lint = by_cmd["lint"]
    assert lint.rerun_hint == []
    assert lint.searched_run_dirs == []


def test_inherited_gate_marks_source_run(monkeypatch) -> None:
    proj = _projection(
        gates=(
            _gate(
                command="unit", status="PASS",
                receipt_path="/runs/parent/verification_command_receipts/unit.json",
                source_run_id="parent_run",
                inherited=True,
            ),
        ),
        inherited=("unit from run parent_run (/runs/parent/.../unit.json)",),
        searched_run_dirs=("/runs/rid", "/runs/parent"),
    )
    _patch(monkeypatch, proj)

    tl = inspect_run_evidence("rid", slice="verification_timeline").verification_timeline
    unit = tl.gates[0]
    assert unit.inherited is True
    assert unit.source_run_id == "parent_run"
    assert "/runs/parent" in tl.searched_run_dirs


def test_autorun_events_project_fresh_and_pass(monkeypatch) -> None:
    from sdk import AutorunEvent

    proj = _projection(
        gates=(
            _gate(command="lint", status="FRESH"),
            _gate(command="unit", status="PASS"),
        ),
        autorun_events=(
            AutorunEvent(
                phase="final_acceptance",
                source="stage9_autorun",
                hook_label="pre-final auto-run",
                ran_pass=("unit",),
                ran_fail=(),
                skipped_fresh=("lint",),
                skipped_manual=("manual_gate",),
                receipt_paths=("/runs/rid/verification_command_receipts/unit.json",),
            ),
        ),
    )
    _patch(monkeypatch, proj)

    tl = inspect_run_evidence("rid", slice="verification_timeline").verification_timeline
    assert len(tl.autorun_events) == 1
    ev = tl.autorun_events[0]
    assert ev.source == "stage9_autorun"
    assert ev.hook_label == "pre-final auto-run"
    assert ev.ran_pass == ["unit"]
    assert ev.skipped_fresh == ["lint"]
    assert ev.skipped_manual == ["manual_gate"]


def test_empty_projection_without_contract(monkeypatch) -> None:
    proj = _projection(has_contract=False, gates=())
    _patch(monkeypatch, proj)

    tl = inspect_run_evidence("rid", slice="verification_timeline").verification_timeline
    assert tl is not None
    assert tl.has_contract is False
    assert tl.gates == []
    assert tl.residual_missing == []
    assert tl.scheduled_trail_available is False


def test_optional_empty_strings_collapse_to_none(monkeypatch) -> None:
    # An SDK gate with empty env/hook/receipt_path/source_run_id/stale_reason
    # projects those Optionals to None on the wire.
    proj = _projection(
        gates=(
            _gate(
                command="unit", env="", hook="", status="MISSING",
                policy="warn", receipt_path="", source_run_id="", stale_reason="",
                searched_run_dirs=("/runs/rid",),
                rerun_hint=("orcho verify run --required --run-id rid --project /proj",),
            ),
        ),
        residual_missing=("unit",),
    )
    _patch(monkeypatch, proj)

    tl = inspect_run_evidence("rid", slice="verification_timeline").verification_timeline
    unit = tl.gates[0]
    assert unit.env is None
    assert unit.hook is None
    assert unit.receipt_path is None
    assert unit.source_run_id is None
    assert unit.stale_reason is None
    assert unit.source is None


def test_all_slice_includes_verification_timeline(monkeypatch) -> None:
    proj = _projection(gates=(_gate(command="lint", status="PASS"),))
    _patch(monkeypatch, proj)
    # Stub the other SDK seams so slice="all" does not touch a real run.
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
    monkeypatch.setattr(ev, "find_run_dir", lambda run_id: __import__("pathlib").Path("/tmp"))

    result = inspect_run_evidence("rid", slice="all")
    assert result.verification_timeline is not None
    assert result.verification_timeline.gates[0].command == "lint"


def test_slice_isolates_other_slices(monkeypatch) -> None:
    proj = _projection(gates=(_gate(command="lint", status="PASS"),))
    _patch(monkeypatch, proj)

    result = inspect_run_evidence("rid", slice="verification_timeline")
    assert result.verification_timeline is not None
    assert result.plan is None
    assert result.findings is None
    assert result.verification_receipts is None


def test_invalid_slice_rejected() -> None:
    with pytest.raises(InvalidPlanError):
        inspect_run_evidence("rid", slice="bogus")


def test_unknown_run_maps_to_run_not_found(monkeypatch) -> None:
    from sdk import RunNotFound

    def _raise(*, run_id, **_):
        raise RunNotFound(f"No run directory: {run_id}")

    monkeypatch.setattr(_SEAM, _raise)
    with pytest.raises(RunNotFoundError):
        inspect_run_evidence("nope", slice="verification_timeline")


def test_stale_core_without_sdk_symbol_raises_clear_error_on_explicit_slice(
    monkeypatch,
) -> None:
    # Simulate a version-skewed / stale orcho-core that predates
    # ``sdk.get_verification_timeline``: the module binds the alias to None.
    # An EXPLICIT slice request must fail loud with an actionable message
    # (not a raw ImportError / 500).
    monkeypatch.setattr(_SEAM, None)
    with pytest.raises(InvalidPlanError) as exc:
        inspect_run_evidence("rid", slice="verification_timeline")
    assert "get_verification_timeline" in str(exc.value)


def test_stale_core_all_slice_also_fails_loud(monkeypatch) -> None:
    # ``verification_timeline`` is a REQUIRED slice of ``slice="all"``. When the
    # connected core cannot serve it, ``all`` must NOT silently drop it (that
    # would make the bundle lie about completeness) — it fails loud with the
    # same actionable diagnostic as the explicit request.
    monkeypatch.setattr(_SEAM, None)
    with pytest.raises(InvalidPlanError) as exc:
        inspect_run_evidence("rid", slice="all")
    assert "get_verification_timeline" in str(exc.value)


def test_stale_core_unrelated_slice_still_serves(monkeypatch) -> None:
    # The defensive import keeps the module loadable, so a slice that does NOT
    # request verification_timeline still serves normally even when the SDK
    # symbol is absent — the boundary is "module survives + unrelated slices
    # work", NOT "silently drop a requested required slice".
    import orcho_mcp.inspection.evidence as ev

    monkeypatch.setattr(_SEAM, None)
    monkeypatch.setattr(ev, "_sdk_get_plan_summary", lambda *a, **k: _PLAN_STUB())

    result = inspect_run_evidence("rid", slice="plan")
    assert result.plan is not None
    assert result.verification_timeline is None


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
