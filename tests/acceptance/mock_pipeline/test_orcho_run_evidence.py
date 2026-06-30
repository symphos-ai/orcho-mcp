"""Acceptance tests for `orcho_run_evidence` inspection slices.

Pins the wire contract: a control-loop client should be able to
understand a run *without reading raw logs*. Each slice projection
is exercised against real (mock) pipeline output.

  - ``slice="all"`` returns every slice in one call.
  - ``slice="plan" / "findings" / "commands" / "artifacts" / "errors"
    / "sub_runs"`` returns just that slice; others stay None.
  - ``severity_min`` and ``phases`` filters apply only to findings.
  - Invalid ``slice`` / ``severity_min`` values raise structured errors.
  - Halt reasons (e.g. ``plan_rejected``) surface through the errors
    slice without log inspection.

Marked ``mcp_integration``; enable with ``pytest -m mcp_integration``.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.mcp_integration


async def _wait_status(
    run_id: str,
    expected: set[str],
    timeout_s: float = 60.0,
) -> str:
    from orcho_mcp.tools import orcho_run_status
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = orcho_run_status(run_id)
        cur = (snap.meta or {}).get("status")
        if cur in expected:
            return cur
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"run {run_id} did not reach {expected!r} within {timeout_s}s"
    )


# ── slice="all" — one-shot inspection ───────────────────────────────────────


@pytest.mark.asyncio
async def test_slice_all_populates_every_slice(mock_project: Path) -> None:
    """A finished mock run yields every slice typed and non-None."""
    from orcho_mcp.tools import orcho_run_evidence, orcho_run_start

    started = await orcho_run_start(
        task="inspect-all",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    await _wait_status(started.run_id, {"done"})

    result = orcho_run_evidence(started.run_id, slice="all")

    assert result.run_id == started.run_id
    assert result.slice == "all"

    # Every slice populated (lists may be empty for fields the mock
    # doesn't emit, but the slice itself is not None).
    assert result.plan is not None
    assert result.findings is not None
    assert result.commands is not None
    assert result.artifacts is not None
    assert result.errors is not None
    assert result.sub_runs is not None


# ── individual slices ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slice_plan_only(mock_project: Path) -> None:
    """``slice="plan"`` populates plan; other slices stay None."""
    from orcho_mcp.tools import orcho_run_evidence, orcho_run_start

    started = await orcho_run_start(
        task="plan slice",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    await _wait_status(started.run_id, {"done"})

    result = orcho_run_evidence(started.run_id, slice="plan")
    assert result.slice == "plan"
    assert result.plan is not None
    assert isinstance(result.plan.short_summary, str)
    assert isinstance(result.plan.subtask_count, int)
    # Every other slice stays None.
    assert result.findings is None
    assert result.commands is None
    assert result.artifacts is None
    assert result.errors is None
    assert result.sub_runs is None


@pytest.mark.asyncio
async def test_slice_errors_only(mock_project: Path) -> None:
    from orcho_mcp.tools import orcho_run_evidence, orcho_run_start

    started = await orcho_run_start(
        task="errors slice",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    await _wait_status(started.run_id, {"done"})

    result = orcho_run_evidence(started.run_id, slice="errors")
    assert result.errors is not None
    assert result.errors.status == "done"  # mock pipeline reaches done
    assert result.errors.halt_reason is None
    assert result.plan is None
    assert result.findings is None


@pytest.mark.asyncio
async def test_slice_sub_runs_empty_for_single_project(
    mock_project: Path,
) -> None:
    """Single-project runs have no sub-runs; slice returns empty list."""
    from orcho_mcp.tools import orcho_run_evidence, orcho_run_start

    started = await orcho_run_start(
        task="sub_runs single",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    await _wait_status(started.run_id, {"done"})

    result = orcho_run_evidence(started.run_id, slice="sub_runs")
    assert result.sub_runs == []


# ── halted-run inspection (plan_rejected → errors slice surfaces it) ──────────


@pytest.mark.asyncio
async def test_halted_run_surfaces_halt_reason_in_errors_slice(
    mock_project: Path,
) -> None:
    """A run halted via ``orcho_phase_handoff_decide(..., action="halt")``
    shows ``halt_reason="phase_handoff_halt"`` through
    ``orcho_run_evidence(slice="errors")`` without the client reading
    meta.json or runner.log directly."""
    from orcho_mcp.tools import (
        orcho_phase_handoff_decide,
        orcho_run_evidence,
        orcho_run_start,
        orcho_run_status,
    )

    started = await orcho_run_start(
        task="evidence over halted run",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
        mock_validate_plan_reject=3,
    )
    await _wait_status(started.run_id, {"awaiting_phase_handoff"})

    snap = orcho_run_status(started.run_id)
    handoff = (snap.meta or {})["phase_handoff"]
    await orcho_phase_handoff_decide(
        started.run_id,
        handoff_id=handoff["id"],
        action="halt",
        note="not salvageable",
    )

    result = orcho_run_evidence(started.run_id, slice="errors")
    assert result.errors is not None
    assert result.errors.status == "halted"
    assert result.errors.halt_reason == "phase_handoff_halt"
    assert result.errors.halted_at is not None


# ── findings filters ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_severity_min_filter_passes_through_to_sdk(
    mock_project: Path,
) -> None:
    """Severity filter is honoured; "P0" returns at most P0 findings."""
    from orcho_mcp.tools import orcho_run_evidence, orcho_run_start

    started = await orcho_run_start(
        task="findings severity filter",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
        mock_validate_plan_reject=2,
    )
    # Mock plan_qa rejection emits findings at various severities.
    await _wait_status(started.run_id, {"awaiting_phase_handoff"})

    everything = orcho_run_evidence(started.run_id, slice="findings")
    assert everything.findings is not None

    p0_only = orcho_run_evidence(
        started.run_id, slice="findings", severity_min="P0",
    )
    assert p0_only.findings is not None
    for f in p0_only.findings:
        assert f.severity == "P0", (
            f"severity_min=P0 returned {f.severity!r}"
        )


@pytest.mark.asyncio
async def test_phases_filter_restricts_to_chosen_phases(
    mock_project: Path,
) -> None:
    """``phases=["validate_plan"]`` returns only plan_qa findings."""
    from orcho_mcp.tools import orcho_run_evidence, orcho_run_start

    started = await orcho_run_start(
        task="phase filter",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
        mock_validate_plan_reject=2,
    )
    await _wait_status(started.run_id, {"awaiting_phase_handoff"})

    only_plan_qa = orcho_run_evidence(
        started.run_id, slice="findings", phases=["validate_plan"],
    )
    assert only_plan_qa.findings is not None
    for f in only_plan_qa.findings:
        assert f.phase == "validate_plan", (
            f"phases=['plan_qa'] returned phase={f.phase!r}"
        )


# ── error contracts ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_slice_raises_invalid_plan_error(
    mock_project: Path,
) -> None:
    from orcho_mcp.errors import InvalidPlanError
    from orcho_mcp.tools import orcho_run_evidence, orcho_run_start

    started = await orcho_run_start(
        task="invalid slice",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    await _wait_status(started.run_id, {"done"})

    with pytest.raises(InvalidPlanError) as exc:
        orcho_run_evidence(started.run_id, slice="logs")
    assert "slice" in str(exc.value).lower()
    assert "plan" in str(exc.value)


@pytest.mark.asyncio
async def test_invalid_severity_min_raises_invalid_plan_error(
    mock_project: Path,
) -> None:
    from orcho_mcp.errors import InvalidPlanError
    from orcho_mcp.tools import orcho_run_evidence, orcho_run_start

    started = await orcho_run_start(
        task="invalid severity",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    await _wait_status(started.run_id, {"done"})

    with pytest.raises(InvalidPlanError) as exc:
        orcho_run_evidence(
            started.run_id, slice="findings", severity_min="critical",
        )
    assert "P0" in str(exc.value)


@pytest.mark.asyncio
async def test_unknown_run_id_raises_run_not_found(
    mock_project: Path,
) -> None:
    from orcho_mcp.errors import RunNotFoundError
    from orcho_mcp.tools import orcho_run_evidence

    with pytest.raises(RunNotFoundError) as exc:
        orcho_run_evidence("does_not_exist_29990101_000000")
    assert "does_not_exist_29990101_000000" in str(exc.value)


# ── receipts slice (P7 done-criteria attestation) ───────────────────────────


@pytest.mark.asyncio
async def test_slice_receipts_surfaces_done_criteria_attestation(
    mock_project: Path,
) -> None:
    """A finished ``subtask_dag`` mock run (default ``feature`` profile)
    surfaces per-subtask receipts through ``slice="receipts"``, each carrying
    the P7 done-criteria self-attestation the mock developer emitted."""
    from orcho_mcp.tools import orcho_run_evidence, orcho_run_start

    started = await orcho_run_start(
        task="receipts attestation",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    await _wait_status(started.run_id, {"done"})

    result = orcho_run_evidence(started.run_id, slice="receipts")
    assert result.slice == "receipts"
    assert result.receipts is not None
    assert len(result.receipts) >= 1
    # The mock developer closes every done-criterion, so a clean run is all
    # ``done`` with no attestation gate firing.
    assert all(r.state == "done" for r in result.receipts)
    assert all(r.attestation_error is None for r in result.receipts)

    # At least one subtask declared done-criteria, so its receipt carries a
    # typed criteria_report + summary (criteria-less subtasks carry neither).
    with_criteria = [r for r in result.receipts if r.criteria_report]
    assert with_criteria, "expected a receipt carrying a done-criteria report"
    r = with_criteria[0]
    assert r.attestation_summary
    assert all(c.met for c in r.criteria_report)
    assert all(c.evidence for c in r.criteria_report)
    assert [c.index for c in r.criteria_report] == list(
        range(1, len(r.criteria_report) + 1)
    )

    # Slice isolation: only receipts populated.
    assert result.plan is None
    assert result.findings is None


@pytest.mark.asyncio
async def test_slice_all_includes_receipts(mock_project: Path) -> None:
    """``slice="all"`` populates the receipts slice alongside the others."""
    from orcho_mcp.tools import orcho_run_evidence, orcho_run_start

    started = await orcho_run_start(
        task="all includes receipts",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    await _wait_status(started.run_id, {"done"})

    result = orcho_run_evidence(started.run_id, slice="all")
    assert result.receipts is not None
    assert len(result.receipts) >= 1
