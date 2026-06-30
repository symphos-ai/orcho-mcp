"""Unit tests for the ``orcho_run_evidence(slice="receipts")`` projection.

Pins the MCP projection layer for P7 subtask delivery receipts: the
SDK accessor (``sdk.list_subtask_receipts``, tested in orcho-core) is
monkeypatched at this module's own seam, so these tests cover the
wire-record mapping only — done vs. ``incomplete`` state, the
done-criteria attestation (``criteria_report`` / ``attestation_summary``
/ ``attestation_error``), and slice isolation.
"""
from __future__ import annotations

import pytest

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.inspection.evidence import inspect_run_evidence

_SEAM = "orcho_mcp.inspection.evidence._sdk_list_subtask_receipts"


def _fake_receipts():
    from sdk import CriterionReport, SubtaskReceipt
    return [
        SubtaskReceipt(
            subtask_id="t1",
            state="done",
            runtime="claude",
            model="m",
            skill=None,
            depends_on=(),
            done_criteria=("a", "b"),
            duration=1.5,
            error=None,
            criteria_report=(
                CriterionReport(index=1, criterion="a", met=True, evidence="did a"),
                CriterionReport(index=2, criterion="b", met=True, evidence="did b"),
            ),
            attestation_summary="all met",
            attestation_error=None,
            attestation_repaired=True,
        ),
        SubtaskReceipt(
            subtask_id="t2",
            state="incomplete",
            runtime="claude",
            model="m",
            skill=None,
            depends_on=("t1",),
            done_criteria=("c",),
            duration=0.5,
            error=None,
            criteria_report=(),
            attestation_summary=None,
            attestation_error="done_criteria not met (by index): [1]",
            attestation_repaired=False,
        ),
    ]


def test_receipts_slice_projects_done_and_incomplete(monkeypatch) -> None:
    monkeypatch.setattr(_SEAM, lambda run_id, cwd=None: _fake_receipts())

    result = inspect_run_evidence("rid", slice="receipts")

    assert result.slice == "receipts"
    assert result.receipts is not None
    by_id = {r.subtask_id: r for r in result.receipts}

    done = by_id["t1"]
    assert done.state == "done"
    assert done.done_criteria == ["a", "b"]
    assert done.attestation_summary == "all met"
    assert done.attestation_error is None
    assert done.attestation_repaired is True
    assert [c.index for c in done.criteria_report] == [1, 2]
    assert done.criteria_report[0].met is True
    assert done.criteria_report[0].evidence == "did a"

    incomplete = by_id["t2"]
    assert incomplete.state == "incomplete"
    assert incomplete.depends_on == ["t1"]
    assert incomplete.criteria_report == []
    assert incomplete.attestation_error == "done_criteria not met (by index): [1]"
    assert incomplete.attestation_repaired is False


def test_receipts_slice_isolates_other_slices(monkeypatch) -> None:
    monkeypatch.setattr(_SEAM, lambda run_id, cwd=None: _fake_receipts())

    result = inspect_run_evidence("rid", slice="receipts")

    # Only the requested slice is populated; the others stay None and their
    # SDK accessors are never called (no real run on disk here).
    assert result.receipts is not None
    assert result.plan is None
    assert result.findings is None
    assert result.commands is None
    assert result.artifacts is None
    assert result.errors is None
    assert result.sub_runs is None


def test_receipts_slice_empty_list_is_not_none(monkeypatch) -> None:
    monkeypatch.setattr(_SEAM, lambda run_id, cwd=None: [])

    result = inspect_run_evidence("rid", slice="receipts")
    assert result.receipts == []


def test_invalid_slice_still_rejected() -> None:
    with pytest.raises(InvalidPlanError):
        inspect_run_evidence("rid", slice="bogus")
