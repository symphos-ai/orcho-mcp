"""The cockpit is the same canonical scheduled-gate ledger projection."""

from __future__ import annotations

from orcho_mcp.inspection.evidence import inspect_run_evidence

_SEAM = "orcho_mcp.inspection.evidence._sdk_get_verification_timeline"


def test_cockpit_and_timeline_share_one_sdk_read_and_wire_record(monkeypatch) -> None:
    from sdk.verification_timeline import (
        ScheduledGateEvent,
        ScheduledGateRow,
        VerificationTimelineProjection,
    )

    projection = VerificationTimelineProjection(
        schema_version="1",
        run_id="rid",
        project="/project",
        finalized=True,
        rows=(
            ScheduledGateRow(
                command="unit",
                hook="after_phase",
                phase="implement",
                declared=True,
                selectable=True,
                selected=True,
                execution_policy="warn",
                consequence="warning",
                disposition="executed_fail",
                selection_reason=None,
                executor="engine",
                trigger="after_phase",
            ),
        ),
        events=(
            ScheduledGateEvent(
                command="unit",
                hook="after_phase",
                phase="implement",
                kind="execution",
                outcome="fail",
                reason="exit 1",
            ),
        ),
    )
    calls = 0

    def read_once(*, run_id, **_):
        nonlocal calls
        calls += 1
        return projection

    monkeypatch.setattr(_SEAM, read_once)

    from sdk import ErrorsAndHalt, PlanSummary

    import orcho_mcp.inspection.evidence as evidence

    monkeypatch.setattr(
        evidence,
        "_sdk_get_plan_summary",
        lambda *args, **kwargs: PlanSummary(
            source="plan",
            short_summary="",
            planning_context="",
            subtask_count=0,
            has_contract=False,
            goal="",
            acceptance_criteria=(),
            owned_files=(),
            commands_to_run=(),
            risks=(),
            review_focus=(),
        ),
    )
    for seam in (
        "_sdk_list_findings",
        "_sdk_list_evidence_commands",
        "_sdk_list_evidence_artifacts",
        "_sdk_list_sub_runs",
        "_sdk_list_subtask_receipts",
    ):
        monkeypatch.setattr(evidence, seam, lambda *args, **kwargs: [])
    monkeypatch.setattr(
        evidence,
        "_sdk_get_errors_halt",
        lambda *args, **kwargs: ErrorsAndHalt(
            status="done", errors=(), halt_reason=None, halted_at=None,
            error_summary=None,
        ),
    )
    monkeypatch.setattr(evidence, "_sdk_list_handoff_advice", lambda *args, **kwargs: None)
    monkeypatch.setattr(evidence, "_read_verification_receipts", lambda run_dir: [])
    monkeypatch.setattr(evidence, "find_run_dir", lambda run_id: __import__("pathlib").Path("/tmp"))

    result = inspect_run_evidence("rid", slice="all")

    assert calls == 1
    assert result.verification_cockpit is not None
    assert result.verification_timeline is not None
    assert result.verification_cockpit.model_dump() == result.verification_timeline.model_dump()
    assert result.verification_cockpit.model_dump() == {
        "schema_version": "1",
        "run_id": "rid",
        "project": "/project",
        "finalized": True,
        "rows": [
            {
                "command": "unit",
                "hook": "after_phase",
                "phase": "implement",
                "declared": True,
                "selectable": True,
                "selected": True,
                "execution_policy": "warn",
                "consequence": "warning",
                "disposition": "executed_fail",
                "selection_reason": None,
                "executor": "engine",
                "trigger": "after_phase",
                "receipt_evidence": None,
            }
        ],
        "events": [
            {
                "command": "unit",
                "hook": "after_phase",
                "phase": "implement",
                "kind": "execution",
                "outcome": "fail",
                "reason": "exit 1",
                "receipt_evidence": None,
            }
        ],
    }
