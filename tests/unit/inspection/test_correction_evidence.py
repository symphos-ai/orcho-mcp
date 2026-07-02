"""Unit tests for the ``delivery`` and ``correction`` evidence slices.

Pins the MCP projection layer for the post-release commit-delivery outcome
(``orcho_run_evidence(slice="delivery")``) and the ADR 0098 correction
fixed-point / non-convergence block (``slice="correction"``). Both read
durable meta via ``services.run_artifacts.get_run_meta_raw`` (exercised here
through a real synthetic run dir under ``fake_workspace``); the delivery
projection additionally reuses ``_project_implement_delivery`` over the SAME
errors-rollup the ``errors`` slice surfaces (single source).

The ``delivery`` slice is read-only evidence — it never resolves actions or
mutates state (that is the interactive ``orcho_delivery_gate``). The
``correction`` slice expresses ``non_converging`` as an operator-decision
condition; ``suggested_actions`` are advisory, never auto-applied.
"""
from __future__ import annotations

import pytest

from orcho_mcp.inspection.evidence import inspect_run_evidence
from tests.fixtures.mcp_workspace import meta, write_run

_ERRORS_SEAM = "orcho_mcp.inspection.evidence._sdk_get_errors_halt"


# ── delivery ─────────────────────────────────────────────────────────────────


def test_delivery_approved_committed(fake_workspace) -> None:
    write_run(
        fake_workspace, "20260201_000001",
        meta=meta(
            status="done",
            commit_delivery={
                "status": "committed",
                "action": "approve",
                "release_verdict": "approved",
                "commit_sha": "abc1234",
            },
        ),
    )

    r = inspect_run_evidence("20260201_000001", slice="delivery")

    assert r.slice == "delivery"
    d = r.delivery
    assert d is not None
    assert d.release_verdict == "approved"
    assert d.decision_status == "committed"
    assert d.action == "approve"
    assert d.applied is True
    assert d.committed is True
    assert d.commit_sha == "abc1234"
    assert d.skipped is False
    assert d.failed is False


def test_delivery_applied_uncommitted(fake_workspace) -> None:
    """``applied_uncommitted`` — the diff landed but no commit was written."""
    write_run(
        fake_workspace, "20260201_000002",
        meta=meta(
            status="done",
            commit_delivery={
                "status": "applied_uncommitted",
                "action": "apply",
                "release_verdict": "approved",
            },
        ),
    )

    d = inspect_run_evidence("20260201_000002", slice="delivery").delivery
    assert d is not None
    assert d.applied is True
    assert d.committed is False
    assert d.commit_sha is None
    assert d.skipped is False
    assert d.failed is False


def test_delivery_skipped(fake_workspace) -> None:
    write_run(
        fake_workspace, "20260201_000003",
        meta=meta(
            status="done",
            commit_delivery={"status": "skipped", "action": "skip"},
        ),
    )

    d = inspect_run_evidence("20260201_000003", slice="delivery").delivery
    assert d is not None
    assert d.skipped is True
    assert d.applied is False
    assert d.committed is False
    assert d.failed is False


@pytest.mark.parametrize(
    "status",
    ["commit_failed", "apply_failed", "halted", "verification_blocked",
     "target_dirty"],
)
def test_delivery_failed_statuses(fake_workspace, status) -> None:
    write_run(
        fake_workspace, f"20260201_0001_{status}",
        meta=meta(
            status="halted",
            halt_reason="commit_delivery_failed",
            commit_delivery={"status": status, "action": "approve"},
        ),
    )

    d = inspect_run_evidence(f"20260201_0001_{status}", slice="delivery").delivery
    assert d is not None
    assert d.failed is True
    assert d.applied is False
    assert d.committed is False
    assert d.skipped is False
    assert d.halt_reason == "commit_delivery_failed"


def test_delivery_rejected_correction_requested(fake_workspace) -> None:
    """A rejected release with a ``fix_requested`` delivery reads rejected;
    ``fix_requested`` is a correction-flow state, not a delivery failure."""
    write_run(
        fake_workspace, "20260201_000004",
        meta=meta(
            status="awaiting_delivery_decision",
            commit_delivery={
                "status": "fix_requested",
                "action": "fix",
                "release_verdict": "rejected",
            },
        ),
    )

    d = inspect_run_evidence("20260201_000004", slice="delivery").delivery
    assert d is not None
    assert d.release_verdict == "rejected"
    assert d.decision_status == "fix_requested"
    assert d.action == "fix"
    assert d.applied is False
    assert d.committed is False
    assert d.skipped is False
    assert d.failed is False


def test_delivery_gate_rerun_child_reads_approved(fake_workspace) -> None:
    """A correction child re-run after a ``gate_rerun`` reads ``approved`` from
    its OWN commit_delivery block (release_verdict), distinct from the parent's
    earlier rejection."""
    write_run(
        fake_workspace, "20260201_000005",
        meta=meta(
            status="done",
            commit_delivery={
                "status": "committed",
                "action": "approve",
                "release_verdict": "approved",
                "commit_sha": "child9",
            },
        ),
    )

    d = inspect_run_evidence("20260201_000005", slice="delivery").delivery
    assert d is not None
    assert d.release_verdict == "approved"
    assert d.committed is True
    assert d.commit_sha == "child9"


def test_delivery_unknown_status_all_false(fake_workspace) -> None:
    """An unrecognized status leaves all four booleans False; raw status kept."""
    write_run(
        fake_workspace, "20260201_000006",
        meta=meta(
            status="running",
            commit_delivery={"status": "brand_new_status"},
        ),
    )

    d = inspect_run_evidence("20260201_000006", slice="delivery").delivery
    assert d is not None
    assert d.decision_status == "brand_new_status"
    assert d.applied is False
    assert d.committed is False
    assert d.skipped is False
    assert d.failed is False
    assert d.release_verdict == "none"


def test_delivery_reuses_implement_delivery_from_errors(
    fake_workspace, monkeypatch,
) -> None:
    """``implement_delivery`` is the SAME projection as the errors slice, built
    from the errors-rollup — proving single source, not a second meta read."""
    from sdk import ErrorsAndHalt

    write_run(
        fake_workspace, "20260201_000007",
        meta=meta(
            status="done",
            commit_delivery={"status": "committed", "commit_sha": "x1"},
        ),
    )
    rollup = (
        {
            "kind": "implement_delivery",
            "delivery_status": "waived",
            "delivery_waived": True,
            "waiver_id": "implement:auto",
            "action": "continue_with_waiver",
            "incomplete_subtasks": ["T9"],
        },
    )
    monkeypatch.setattr(
        _ERRORS_SEAM,
        lambda run_id, cwd=None: ErrorsAndHalt(
            status="done", errors=rollup, halt_reason=None, halted_at=None,
            error_summary=None,
        ),
    )

    d = inspect_run_evidence("20260201_000007", slice="delivery").delivery
    assert d is not None
    assert d.implement_delivery is not None
    assert d.implement_delivery.delivery_status == "waived"
    assert d.implement_delivery.incomplete_subtasks == ["T9"]


def test_no_commit_delivery_yields_none(fake_workspace) -> None:
    write_run(
        fake_workspace, "20260201_000008",
        meta=meta(status="done"),
    )

    r = inspect_run_evidence("20260201_000008", slice="delivery")
    assert r.delivery is None


# ── correction ───────────────────────────────────────────────────────────────


def test_correction_non_converging(fake_workspace) -> None:
    write_run(
        fake_workspace, "20260201_000101",
        meta=meta(
            status="halted",
            halt_reason="correction_not_converging",
            correction_fixed_point={
                "repeated": ["blocker-a", "blocker-b"],
                "parent_run_id": "20260201_000100",
                "child_run_id": "20260201_000101",
                "suggested_actions": [
                    "Inspect the recurring blockers manually.",
                    "Stop the correction loop.",
                ],
                "reason": "child repeated the parent's release blockers",
            },
        ),
    )

    r = inspect_run_evidence("20260201_000101", slice="correction")

    assert r.slice == "correction"
    c = r.correction
    assert c is not None
    assert c.non_converging is True
    assert c.repeated == ["blocker-a", "blocker-b"]
    assert c.parent_run_id == "20260201_000100"
    assert c.child_run_id == "20260201_000101"
    assert c.suggested_actions == [
        "Inspect the recurring blockers manually.",
        "Stop the correction loop.",
    ]
    assert c.reason == "child repeated the parent's release blockers"


def test_no_correction_fixed_point_yields_none(fake_workspace) -> None:
    write_run(
        fake_workspace, "20260201_000102",
        meta=meta(status="done"),
    )

    r = inspect_run_evidence("20260201_000102", slice="correction")
    assert r.correction is None


# ── slice="all" inclusion ────────────────────────────────────────────────────


def test_all_slice_includes_delivery_and_correction(fake_workspace) -> None:
    write_run(
        fake_workspace, "20260201_000201",
        meta=meta(
            status="halted",
            halt_reason="correction_not_converging",
            commit_delivery={
                "status": "committed",
                "release_verdict": "approved",
                "commit_sha": "z9",
            },
            correction_fixed_point={
                "repeated": ["b1"],
                "parent_run_id": "p",
                "child_run_id": "c",
                "suggested_actions": ["stop"],
                "reason": "loop",
            },
        ),
    )

    r = inspect_run_evidence("20260201_000201", slice="all")

    assert r.delivery is not None
    assert r.delivery.release_verdict == "approved"
    assert r.delivery.committed is True
    assert r.correction is not None
    assert r.correction.non_converging is True
    assert r.correction.repeated == ["b1"]
