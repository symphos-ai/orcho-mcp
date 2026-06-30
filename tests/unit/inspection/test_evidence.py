"""Unit tests for the ``orcho_run_evidence(slice="errors")`` projection,
focused on the typed delivery/waiver audit (``implement_delivery``).

Pins the MCP projection layer: the SDK accessor
(``sdk.get_errors_halt``, tested in orcho-core) is monkeypatched at this
module's own seam, so these tests cover the wire-record mapping only.
The projector reads the SAME errors-rollup the raw ``errors[]`` field
exposes, so a waived delivery round-trips every field and a clean
delivery yields ``implement_delivery=None``.
"""
from __future__ import annotations

from sdk import ErrorsAndHalt

from orcho_mcp.inspection.evidence import inspect_run_evidence

_SEAM = "orcho_mcp.inspection.evidence._sdk_get_errors_halt"


def _errors_halt(errors: tuple[dict, ...]) -> ErrorsAndHalt:
    return ErrorsAndHalt(
        status="awaiting_phase_handoff",
        errors=errors,
        halt_reason=None,
        halted_at=None,
        error_summary=None,
    )


def test_errors_slice_projects_waived_delivery(monkeypatch) -> None:
    """A rollup carrying ``implement_delivery`` + ``phase_handoff_waiver``
    breadcrumbs round-trips every field into ``ImplementDeliveryRecord``."""
    rollup = (
        {
            "kind": "implement_delivery",
            "delivery_status": "waived",
            "delivery_waived": True,
            "waiver_id": "validate_plan:plan_round:2",
            "action": "continue_with_waiver",
            "incomplete_subtasks": ["T2", "T3"],
            "missing_subtask_receipts": ["T4"],
        },
        {"kind": "phase_handoff_waiver", "decided_by": "operator"},
    )
    monkeypatch.setattr(_SEAM, lambda run_id, cwd=None: _errors_halt(rollup))

    result = inspect_run_evidence("rid", slice="errors")

    assert result.slice == "errors"
    assert result.errors is not None
    # Raw errors[] still carries the breadcrumbs verbatim (single source).
    assert result.errors.errors == list(rollup)

    d = result.errors.implement_delivery
    assert d is not None
    assert d.delivery_status == "waived"
    assert d.delivery_waived is True
    assert d.waiver_id == "validate_plan:plan_round:2"
    assert d.action == "continue_with_waiver"
    assert d.decided_by == "operator"
    assert d.incomplete_subtasks == ["T2", "T3"]
    assert d.missing_subtask_receipts == ["T4"]


def test_errors_slice_clean_delivery_is_none(monkeypatch) -> None:
    """A rollup with no ``implement_delivery`` breadcrumb (clean delivery)
    leaves ``implement_delivery`` as ``None``."""
    rollup = ({"kind": "error", "title": "some unrelated error"},)
    monkeypatch.setattr(_SEAM, lambda run_id, cwd=None: _errors_halt(rollup))

    result = inspect_run_evidence("rid", slice="errors")

    assert result.errors is not None
    assert result.errors.implement_delivery is None


def test_errors_slice_auto_waiver_decided_by(monkeypatch) -> None:
    """The auto path stamps ``decided_by`` on the delivery breadcrumb
    itself (no operator handoff); the projector still surfaces it."""
    rollup = (
        {
            "kind": "implement_delivery",
            "delivery_status": "waived",
            "delivery_waived": True,
            "waiver_id": "implement:auto",
            "action": "continue_with_waiver",
            "decided_by": "auto:on_exhausted",
            "incomplete_subtasks": ["T9"],
        },
    )
    monkeypatch.setattr(_SEAM, lambda run_id, cwd=None: _errors_halt(rollup))

    result = inspect_run_evidence("rid", slice="errors")

    assert result.errors is not None
    d = result.errors.implement_delivery
    assert d is not None
    assert d.decided_by == "auto:on_exhausted"
    assert d.missing_subtask_receipts == []
