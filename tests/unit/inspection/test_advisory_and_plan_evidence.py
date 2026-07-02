"""Unit tests for the advisory-finding flag and PlanSliceRecord.allowed_modifications.

Pins two MCP projection facts:

  * ``FindingRecord.advisory`` — a ``validate_plan`` finding forwarded into a
    successful whole-plan implement (no subtask DAG, no guardrail-block /
    failure) is advisory (visible, NOT an active release blocker). This
    replicates core's ``_implement_whole_plan_delivered`` gate over durable
    meta; the same findings under a subtask DAG stay active.
  * ``PlanSliceRecord.allowed_modifications`` — read from the durable
    ``parsed_plan.json`` top-level field (the SDK plan summary carries none).

Both read a real synthetic run dir under ``fake_workspace``.
"""
from __future__ import annotations

from orcho_mcp.inspection.evidence import inspect_run_evidence
from tests.fixtures.mcp_workspace import meta, write_run


def _validate_plan_finding(fid: str, severity: str = "P1") -> dict:
    return {"id": fid, "severity": severity, "title": f"t-{fid}", "body": "b"}


def _phases_with_findings(*, subtask_count: int | None) -> dict:
    """Meta phases carrying validate_plan + review findings and an implement
    record. ``subtask_count`` None → whole-plan implement; a positive int →
    subtask DAG."""
    implement_meta: dict = {}
    if subtask_count is not None:
        implement_meta["subtask_count"] = subtask_count
    return {
        "validate_plan": [
            {"attempt": 1, "findings": [_validate_plan_finding("VP1")]},
        ],
        "review_changes": [
            {"attempt": 1, "findings": [
                {"id": "R1", "severity": "P1", "title": "rev", "body": "b"},
            ]},
        ],
        "implement": {"output": "delivered whole plan", "meta": implement_meta},
    }


def test_validate_plan_findings_advisory_on_whole_plan(fake_workspace) -> None:
    write_run(
        fake_workspace, "20260301_000001",
        meta=meta(status="done", phases=_phases_with_findings(subtask_count=None)),
    )

    r = inspect_run_evidence("20260301_000001", slice="findings")

    assert r.findings is not None
    by_id = {f.id: f for f in r.findings}
    # validate_plan finding is advisory (visible, not an active blocker).
    assert by_id["VP1"].advisory is True
    # A review finding stays active even under a whole-plan implement.
    assert by_id["R1"].advisory is False
    # Advisory findings are excluded from active release blockers.
    active = [f for f in r.findings if not f.advisory]
    assert "VP1" not in {f.id for f in active}
    assert "R1" in {f.id for f in active}


def test_same_findings_active_on_subtask_dag(fake_workspace) -> None:
    """With a subtask DAG (positive subtask_count) the whole-plan gate is False,
    so the same validate_plan findings stay active (advisory=False)."""
    write_run(
        fake_workspace, "20260301_000002",
        meta=meta(status="done", phases=_phases_with_findings(subtask_count=3)),
    )

    r = inspect_run_evidence("20260301_000002", slice="findings")

    by_id = {f.id: f for f in r.findings}
    assert by_id["VP1"].advisory is False
    assert by_id["R1"].advisory is False


def test_findings_active_when_implement_guardrail_blocked(fake_workspace) -> None:
    """A guardrail-blocked implement is NOT a whole-plan delivery, so
    validate_plan findings remain active."""
    phases = _phases_with_findings(subtask_count=None)
    phases["implement"]["guardrail_blocked"] = True
    write_run(
        fake_workspace, "20260301_000003",
        meta=meta(status="halted", phases=phases),
    )

    r = inspect_run_evidence("20260301_000003", slice="findings")

    by_id = {f.id: f for f in r.findings}
    assert by_id["VP1"].advisory is False


def test_findings_active_when_implement_has_no_output(fake_workspace) -> None:
    """No implement ``output`` → not a whole-plan delivery → findings active."""
    phases = _phases_with_findings(subtask_count=None)
    phases["implement"]["output"] = ""
    write_run(
        fake_workspace, "20260301_000004",
        meta=meta(status="running", phases=phases),
    )

    r = inspect_run_evidence("20260301_000004", slice="findings")

    assert {f.id: f for f in r.findings}["VP1"].advisory is False


def test_old_attempt_findings_not_advisory_when_latest_approved(
    fake_workspace,
) -> None:
    """A multi-attempt validate_plan whose LATEST attempt is approved marks
    nothing advisory — even under a whole-plan implement. Core's
    ``_review_finding_summary`` only ever considers the latest attempt, and skips
    it entirely when approved; the attempt-1 finding is historical/resolved."""
    phases = {
        "validate_plan": [
            {"attempt": 1, "findings": [_validate_plan_finding("VP_OLD")]},
            {"attempt": 2, "verdict": "APPROVED", "findings": []},
        ],
        "implement": {"output": "delivered whole plan", "meta": {}},
    }
    write_run(
        fake_workspace, "20260301_000005",
        meta=meta(status="done", phases=phases),
    )

    r = inspect_run_evidence("20260301_000005", slice="findings")

    by_id = {f.id: f for f in r.findings}
    # Latest attempt approved → old finding is NOT advisory (and not active).
    assert by_id["VP_OLD"].advisory is False


def test_only_latest_attempt_findings_advisory(fake_workspace) -> None:
    """When the latest attempt is NOT approved, only ITS findings are advisory;
    earlier-attempt findings stay non-advisory (historical)."""
    phases = {
        "validate_plan": [
            {"attempt": 1, "findings": [_validate_plan_finding("VP_OLD")]},
            {"attempt": 2, "findings": [_validate_plan_finding("VP_NEW")]},
        ],
        "implement": {"output": "delivered whole plan", "meta": {}},
    }
    write_run(
        fake_workspace, "20260301_000006",
        meta=meta(status="done", phases=phases),
    )

    r = inspect_run_evidence("20260301_000006", slice="findings")

    by_id = {f.id: f for f in r.findings}
    assert by_id["VP_OLD"].advisory is False
    assert by_id["VP_NEW"].advisory is True


def test_non_advisory_set_still_contains_resolved_findings(fake_workspace) -> None:
    """Contract: ``advisory=False`` is NOT the active-blocker set.

    ``sdk.list_findings`` flattens findings across every attempt, so a
    historical/resolved finding (an earlier ``validate_plan`` attempt superseded
    by a later non-approved one) lands in the ``advisory=False`` set even though
    it is not an active release blocker. A captain must therefore not read
    ``active = not advisory``. This pins the wire contract the schema / tools /
    docs describe: ``advisory`` isolates only the forwarded-critique subset."""
    phases = {
        "validate_plan": [
            {"attempt": 1, "findings": [_validate_plan_finding("VP_OLD")]},
            {"attempt": 2, "findings": [_validate_plan_finding("VP_NEW")]},
        ],
        "implement": {"output": "delivered whole plan", "meta": {}},
    }
    write_run(
        fake_workspace, "20260301_000007",
        meta=meta(status="done", phases=phases),
    )

    r = inspect_run_evidence("20260301_000007", slice="findings")

    by_id = {f.id: f for f in r.findings}
    # Only the latest attempt's finding is advisory.
    assert by_id["VP_NEW"].advisory is True
    # The resolved earlier-attempt finding is NOT advisory ...
    assert by_id["VP_OLD"].advisory is False
    # ... yet it is a superseded/historical finding, so the ``advisory=False``
    # set is strictly larger than the active-blocker set. Equating them
    # (``active = not advisory``) would wrongly resurrect VP_OLD as active.
    non_advisory = {f.id for f in r.findings if not f.advisory}
    assert "VP_OLD" in non_advisory


def test_plan_allowed_modifications_from_parsed_plan(fake_workspace) -> None:
    write_run(
        fake_workspace, "20260301_000101",
        meta=meta(status="done"),
        parsed_plan={
            "short_summary": "s",
            "planning_context": "pc",
            "tasks": [],
            "allowed_modifications": ["docs/**", "src/util/*.py"],
        },
    )

    r = inspect_run_evidence("20260301_000101", slice="plan")

    assert r.plan is not None
    assert r.plan.allowed_modifications == ["docs/**", "src/util/*.py"]


def test_plan_allowed_modifications_empty_when_absent(fake_workspace) -> None:
    """No parsed_plan.json (or no allowed_modifications) → empty list, no error."""
    write_run(
        fake_workspace, "20260301_000102",
        meta=meta(status="done"),
    )

    r = inspect_run_evidence("20260301_000102", slice="plan")

    assert r.plan is not None
    assert r.plan.allowed_modifications == []


def test_all_slice_carries_advisory_and_allowed_modifications(fake_workspace) -> None:
    write_run(
        fake_workspace, "20260301_000201",
        meta=meta(status="done", phases=_phases_with_findings(subtask_count=None)),
        parsed_plan={
            "short_summary": "s",
            "planning_context": "pc",
            "tasks": [],
            "allowed_modifications": ["docs/**"],
        },
    )

    r = inspect_run_evidence("20260301_000201", slice="all")

    assert r.plan is not None
    assert r.plan.allowed_modifications == ["docs/**"]
    assert r.findings is not None
    assert {f.id: f for f in r.findings}["VP1"].advisory is True
