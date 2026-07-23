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
from tests.fixtures.mcp_workspace import meta, write_run

_SEAM = "orcho_mcp.inspection.evidence._sdk_get_errors_halt"


def _errors_halt(errors: tuple[dict, ...]) -> ErrorsAndHalt:
    return ErrorsAndHalt(
        status="awaiting_phase_handoff",
        errors=errors,
        halt_reason=None,
        halted_at=None,
        error_summary=None,
    )


def test_errors_slice_uses_settled_supervisor_projection(fake_workspace) -> None:
    run_dir = write_run(
        fake_workspace, "20260701_000001", meta=meta(status="running"),
    )
    (run_dir / "mcp_supervisor.json").write_text(
        '{"run_id":"20260701_000001","pid":7,"status":"failed",'
        '"exit_code":1,"halt_reason":"abnormal_exit:1"}',
        encoding="utf-8",
    )

    result = inspect_run_evidence("20260701_000001", slice="errors")

    assert result.errors.status == "failed"
    assert result.errors.halt_reason == "abnormal_exit:1"


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
    monkeypatch.setattr(_SEAM, lambda run_id, **kwargs: _errors_halt(rollup))

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
    monkeypatch.setattr(_SEAM, lambda run_id, **kwargs: _errors_halt(rollup))

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
    monkeypatch.setattr(_SEAM, lambda run_id, **kwargs: _errors_halt(rollup))

    result = inspect_run_evidence("rid", slice="errors")

    assert result.errors is not None
    d = result.errors.implement_delivery
    assert d is not None
    assert d.decided_by == "auto:on_exhausted"
    assert d.missing_subtask_receipts == []


# ── delivery slice: ADR 0119 delivery_branch / pr_intent ─────────────────────
#
# The projection reads the durable ``meta['commit_delivery']`` decision through
# the SAME ``services.delivery_gate`` helpers the gate projection uses, so the
# ``delivery_branch`` / ``pr_intent`` facts (ADR 0119) are surfaced without a
# second meta read. A worktree_branch *publish* persists ``status='committed'``
# with ``commit_sha=None`` (the commit lands on the run's own branch, not the
# target checkout) plus the delivery branch + PR intent; a ``protect_default`` /
# ``named`` commit persists a real ``commit_sha`` alongside the same branch
# facts. A stale core that emits neither field must degrade to ``None`` — never
# an exception — and keep ``slice='all'`` whole.


def test_delivery_publish_branch_carries_branch_and_pr_intent(fake_workspace) -> None:
    """Publish-only worktree_branch case (ADR 0119): a delivery_branch and a
    typed pr_intent are surfaced, and no commit_sha is fabricated for the
    publish-only branch (the commit landed on the run branch, not the checkout).

    NOTE: core persists ``status='committed'`` for a worktree_branch publish, so
    the implemented mapping (``committed = status == 'committed' or commit_sha``)
    resolves ``committed=True`` here; the load-bearing publish invariant is that
    ``commit_sha`` stays ``None`` (never fabricated)."""
    write_run(
        fake_workspace, "20260202_000010",
        meta=meta(
            status="done",
            commit_delivery={
                "status": "committed",
                "action": "approve",
                "release_verdict": "approved",
                # Publish-only: no commit was written to the target checkout.
                "published_commit_sha": "feed123",
                "delivery_branch": "orcho/deliver/20260202_000010-add-widget",
                "pr_intent": {
                    "branch": "orcho/deliver/20260202_000010-add-widget",
                    "base": "main",
                    "title": "Add widget",
                    "suggested_command": "gh pr create --fill",
                },
            },
        ),
    )

    d = inspect_run_evidence("20260202_000010", slice="delivery").delivery
    assert d is not None
    # ADR 0119 facts surface, typed.
    assert d.delivery_branch == "orcho/deliver/20260202_000010-add-widget"
    assert d.pr_intent is not None
    assert d.pr_intent.branch == "orcho/deliver/20260202_000010-add-widget"
    assert d.pr_intent.base == "main"
    assert d.pr_intent.title == "Add widget"
    assert d.pr_intent.suggested_command == "gh pr create --fill"
    # No fabricated sha for a publish-only branch.
    assert d.commit_sha is None
    assert d.published_commit_sha == "feed123"
    assert d.release_verdict == "approved"


def test_delivery_commit_case_carries_branch_and_sha(fake_workspace) -> None:
    """protect_default / named commit-on-branch case (ADR 0119): a real
    commit_sha is present AND the delivery_branch / pr_intent facts are surfaced;
    the rest of the projection is clean."""
    write_run(
        fake_workspace, "20260202_000011",
        meta=meta(
            status="done",
            commit_delivery={
                "status": "committed",
                "action": "approve",
                "release_verdict": "approved",
                "commit_sha": "deadbee",
                "delivery_branch": "orcho/deliver/20260202_000011-fix-bug",
                "pr_intent": {
                    "branch": "orcho/deliver/20260202_000011-fix-bug",
                    "base": "main",
                    "title": "Fix bug",
                    "suggested_command": "gh pr create --fill",
                },
            },
        ),
    )

    d = inspect_run_evidence("20260202_000011", slice="delivery").delivery
    assert d is not None
    assert d.commit_sha == "deadbee"
    assert d.published_commit_sha is None
    assert d.committed is True
    assert d.applied is True
    assert d.skipped is False
    assert d.failed is False
    # The new branch facts ride alongside the real commit.
    assert d.delivery_branch == "orcho/deliver/20260202_000011-fix-bug"
    assert d.pr_intent is not None
    assert d.pr_intent.title == "Fix bug"


def test_delivery_stale_core_new_fields_none(fake_workspace) -> None:
    """Stale core defensiveness: a ``commit_delivery`` block with NO
    ``delivery_branch`` / ``pr_intent`` keys yields ``None`` for both new fields
    (never an exception), and ``slice='all'`` stays whole."""
    write_run(
        fake_workspace, "20260202_000012",
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

    d = inspect_run_evidence("20260202_000012", slice="delivery").delivery
    assert d is not None
    assert d.delivery_branch is None
    assert d.pr_intent is None
    # Existing fields still project correctly.
    assert d.commit_sha == "abc1234"
    assert d.committed is True

    # slice='all' must not raise and must carry the same None-field delivery.
    all_result = inspect_run_evidence("20260202_000012", slice="all")
    assert all_result.delivery is not None
    assert all_result.delivery.delivery_branch is None
    assert all_result.delivery.pr_intent is None


# ── delivery slice: ADR 0119 pr_url / delivery_notices ───────────────────────
#
# ``pr_url`` and ``delivery_notices`` are read through the SAME shared
# ``services.delivery_gate`` helpers (``_extract_pr_url`` /
# ``_extract_delivery_notices``) the gate projection uses, so the evidence slice
# and the interactive gate never drift. Absent block → ``None`` / ``[]``.


def test_delivery_committed_carries_pr_url_and_notices(fake_workspace) -> None:
    """A committed delivery that opened a PR surfaces ``pr_url`` and the
    human-readable ``delivery_notices`` on the read-only delivery slice."""
    write_run(
        fake_workspace, "20260202_000013",
        meta=meta(
            status="done",
            commit_delivery={
                "status": "committed",
                "action": "approve",
                "release_verdict": "approved",
                "commit_sha": "cafef00",
                "delivery_branch": "orcho/deliver/20260202_000013-ship",
                "pr_url": "https://example.test/pr/77",
                "delivery_notices": [
                    "PR opened: https://example.test/pr/77",
                    "branch orcho/deliver/20260202_000013-ship pushed",
                ],
            },
        ),
    )

    d = inspect_run_evidence("20260202_000013", slice="delivery").delivery
    assert d is not None
    assert d.pr_url == "https://example.test/pr/77"
    assert d.delivery_notices == [
        "PR opened: https://example.test/pr/77",
        "branch orcho/deliver/20260202_000013-ship pushed",
    ]
    assert d.committed is True
    assert d.release_verdict == "approved"


def test_delivery_no_pr_url_or_notices_defaults(fake_workspace) -> None:
    """A commit_delivery block with no ``pr_url`` / ``delivery_notices`` keys
    yields ``None`` / ``[]`` (never an exception)."""
    write_run(
        fake_workspace, "20260202_000014",
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

    d = inspect_run_evidence("20260202_000014", slice="delivery").delivery
    assert d is not None
    assert d.pr_url is None
    assert d.delivery_notices == []
