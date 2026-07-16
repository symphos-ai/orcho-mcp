"""L1 coverage for the canonical scheduled-gate ledger wire projection."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from orcho_mcp.inspection.evidence import inspect_run_evidence

_SEAM = "orcho_mcp.inspection.evidence._sdk_get_verification_timeline"


def _row(**changes):
    from sdk.verification_timeline import ScheduledGateRow

    values = dict(
        command="verify",
        hook="before_phase",
        phase="implement",
        declared=True,
        selectable=True,
        selected=True,
        execution_policy="require",
        consequence="required_action",
        disposition="executed_pass",
        selection_reason=None,
        executor="engine",
        trigger="before_phase",
        receipt_evidence=None,
    )
    values.update(changes)
    return ScheduledGateRow(**values)


def _event(**changes):
    from sdk.verification_timeline import ScheduledGateEvent

    values = dict(
        command="verify",
        hook="before_phase",
        phase="implement",
        kind="execution",
        outcome="pass",
        reason="completed",
        receipt_evidence=None,
    )
    values.update(changes)
    return ScheduledGateEvent(**values)


def _projection(**changes):
    from sdk.verification_timeline import VerificationTimelineProjection

    values = dict(
        schema_version="1",
        run_id="rid",
        project="/project",
        finalized=True,
        rows=(),
        events=(),
    )
    values.update(changes)
    return VerificationTimelineProjection(**values)


def _patch(monkeypatch, projection) -> None:
    monkeypatch.setattr(_SEAM, lambda *, run_id, **_: projection)


@pytest.mark.parametrize(
    ("disposition", "selected", "policy", "consequence"),
    [
        ("not_selected", False, "require", "none"),
        ("manual_available", True, "manual", "none"),
        ("suggested", True, "suggest", "none"),
        ("skipped_fresh", True, "warn", "warning"),
        ("executed_pass", True, "require", "required_action"),
        ("executed_fail", True, "warn", "warning"),
        ("residual_missing", True, "require", "required_action"),
        ("residual_stale", True, "warn", "warning"),
        ("residual_failed", True, "require", "required_action"),
    ],
)
def test_all_sdk_dispositions_are_forwarded(
    monkeypatch,
    disposition,
    selected,
    policy,
    consequence,
) -> None:
    row = _row(
        selected=selected,
        execution_policy=policy,
        consequence=consequence,
        disposition=disposition,
        selection_reason="paths" if disposition == "not_selected" else None,
        executor=None
        if not selected
        else "operator"
        if policy in {"manual", "suggest"}
        else "engine",
        trigger="operator" if policy in {"manual", "suggest"} else "before_phase",
    )
    _patch(monkeypatch, _projection(rows=(row,)))

    result = inspect_run_evidence("rid", slice="verification_timeline")

    assert result.verification_timeline is not None
    assert result.verification_timeline.rows[0].model_dump() == asdict(row)


def test_rows_events_receipt_and_duplicate_command_are_forwarded(monkeypatch) -> None:
    from sdk.verification_timeline import ReceiptEvidence

    receipt = ReceiptEvidence(
        classification="fresh",
        path="receipts/unit.json",
        source="parent",
        inherited=True,
        reason="reused",
        rerun=True,
    )
    projection = _projection(
        rows=(
            _row(command="unit", hook="before_phase", phase="implement", receipt_evidence=receipt),
            _row(
                command="unit",
                hook="before_delivery",
                phase="",
                disposition=None,
                selected=None,
                execution_policy="unknown",
                consequence="none",
                executor=None,
                trigger=None,
            ),
        ),
        events=(_event(command="unit", receipt_evidence=receipt),),
    )
    _patch(monkeypatch, projection)

    timeline = inspect_run_evidence("rid", slice="verification_timeline").verification_timeline

    assert timeline is not None
    assert [(row.command, row.hook, row.phase) for row in timeline.rows] == [
        ("unit", "before_phase", "implement"),
        ("unit", "before_delivery", ""),
    ]
    assert timeline.rows[0].receipt_evidence.model_dump() == asdict(receipt)
    assert timeline.events[0].receipt_evidence.model_dump() == asdict(receipt)
    assert timeline.schema_version == "1"
    assert timeline.project == "/project"
    assert timeline.finalized is True
