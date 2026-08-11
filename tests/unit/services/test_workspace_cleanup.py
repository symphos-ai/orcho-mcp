"""L1 unit — the two-step workspace-cleanup surface.

``orcho_workspace_cleanup_report`` is a pure projection of the engine's
selection, so these tests pin the projection *and* the thing the projection
exists to make possible: a destructive reclaim that cannot run against a
selection nobody reviewed.

The confirmation gate is the reason this surface is two tools rather than
one. On the CLI the operator reads the report on their own screen and then
types the reclaim flags; over MCP the caller is a model, and the protocol
carries no evidence that a human ever saw the selection. So the reclaim is
gated on a token that only a report can mint, and the token is re-derived
from the live workspace before anything is touched — a token that was never
issued, or one issued against a workspace that has since changed, is a
refusal with nothing removed. The tests below drive real engine selection
and a real reclaim rather than stubs, because a stubbed gate would stay
green even if the token were never checked.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from orcho_mcp.errors import WorkspaceCleanupConfirmationError
from orcho_mcp.services.workspace_cleanup import (
    project_workspace_cleanup_report,
    reclaim_workspace_cleanup_confirmed,
)

_EXPIRED = "2020-01-01T00:00:00Z"
_FAR_FUTURE = "2999-01-01T00:00:00Z"


def _run(
    workspace: Path,
    run_id: str,
    *,
    status: str = "done",
    retention_until: str = _EXPIRED,
    checkout: bool = True,
) -> Path:
    """Seed one run with a retained checkout under the fake workspace.

    Mirrors the shape the engine discovers: a run dir with ``meta.json``
    declaring a ``worktree`` block, and the physical checkout it points at.
    Returns the checkout path so a test can assert it survived a refusal.
    """
    runspace = workspace / "runspace"
    run_dir = runspace / "runs" / run_id
    run_dir.mkdir(parents=True)
    worktree = runspace / "worktrees" / run_id / "checkout"
    if checkout:
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: /missing/.git/worktrees/test\n")
        (worktree / "payload.txt").write_text("reclaimable bytes\n")
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "status": status,
                "worktree": {
                    "path": str(worktree),
                    "source_repo_path": str(workspace / "repo"),
                    "retention_until": retention_until,
                },
            }
        )
    )
    return worktree


def _tree(root: Path) -> list[Path]:
    return sorted(path.relative_to(root) for path in root.rglob("*"))


def test_report_projects_every_bucket_and_writes_nothing(fake_workspace) -> None:
    """The preview separates reclaimable, protected, and inert — and is read-only."""
    _run(fake_workspace, "expired")
    _run(fake_workspace, "live", status="running")
    _run(fake_workspace, "already_gone", checkout=False)
    before = _tree(fake_workspace)

    report = project_workspace_cleanup_report()

    assert report.reclaimable_count == 1
    assert report.protected_count == 1
    assert report.inert_count == 1
    assert [row.reason for row in report.reclaimable_reasons] == ["retention_expired"]
    assert [row.reason for row in report.protected_reasons] == ["status_not_stopped"]
    assert [row.reason for row in report.inert_reasons] == ["worktree_gone"]
    assert report.older_than_days == 30
    assert report.force is False
    assert report.runs_dir.endswith("runspace/runs")
    # Read-only is the whole contract of this half: no archive, no receipt,
    # not a single new path anywhere under the workspace.
    assert _tree(fake_workspace) == before


def test_report_points_at_reclaim_only_when_something_is_reclaimable(
    fake_workspace,
) -> None:
    """A next action that suggests removing nothing is worse than no action."""
    _run(fake_workspace, "live", status="running")

    empty = project_workspace_cleanup_report()
    assert empty.reclaimable_count == 0
    assert empty.next_actions == []

    _run(fake_workspace, "expired")
    populated = project_workspace_cleanup_report()

    assert len(populated.next_actions) == 1
    action = populated.next_actions[0]
    assert action.tool == "orcho_workspace_cleanup_reclaim"
    # Never a ready call: tier and disposition are the operator's to choose,
    # and removing a checkout is not something a client should do off a read.
    assert action.kind == "operator_input_required"
    assert action.requires_operator_input is True
    assert action.args["confirm_token"] == populated.confirm_token
    assert "tier" not in action.args
    assert "disposition" not in action.args
    assert sorted(action.input_schema) == ["disposition", "tier"]


def test_reclaim_refuses_a_token_that_was_never_issued(fake_workspace) -> None:
    """The default answer to an unconfirmed destructive call is 'no'."""
    checkout = _run(fake_workspace, "expired")
    before = _tree(fake_workspace)

    with pytest.raises(WorkspaceCleanupConfirmationError):
        reclaim_workspace_cleanup_confirmed(
            tier="worktrees", disposition="delete", confirm_token="wsclean-deadbeef",
        )

    assert checkout.exists()
    assert _tree(fake_workspace) == before


def test_reclaim_refuses_a_token_whose_selection_has_since_changed(
    fake_workspace,
) -> None:
    """The gate protects against a stale review, not just a missing one.

    This is the case a naive "did you pass any token" check would wave
    through: the operator reviewed a report, and by the time the reclaim
    arrives the workspace holds *more* than they agreed to remove.
    """
    reviewed = _run(fake_workspace, "expired_one")
    token = project_workspace_cleanup_report().confirm_token

    # A second run expires between the review and the reclaim.
    appeared_later = _run(fake_workspace, "expired_two")

    with pytest.raises(WorkspaceCleanupConfirmationError) as excinfo:
        reclaim_workspace_cleanup_confirmed(
            tier="worktrees", disposition="delete", confirm_token=token,
        )

    assert "confirm_token" in str(excinfo.value)
    assert reviewed.exists()
    assert appeared_later.exists()


def test_reclaim_refuses_a_token_minted_under_different_arguments(
    fake_workspace,
) -> None:
    """The cutoff is part of what was reviewed, so it is part of the token."""
    _run(fake_workspace, "expired")
    token = project_workspace_cleanup_report(older_than_days=30).confirm_token

    with pytest.raises(WorkspaceCleanupConfirmationError):
        reclaim_workspace_cleanup_confirmed(
            tier="worktrees",
            disposition="delete",
            confirm_token=token,
            older_than_days=7,
        )


def test_matching_token_reclaims_and_returns_the_engine_receipt(
    fake_workspace,
) -> None:
    """The happy path — and proof the gate admits, rather than refusing all."""
    checkout = _run(fake_workspace, "expired")
    report = project_workspace_cleanup_report()

    receipt = reclaim_workspace_cleanup_confirmed(
        tier="worktrees",
        disposition="delete",
        confirm_token=report.confirm_token,
    )

    assert not checkout.exists()
    assert receipt.tier == "worktrees"
    assert receipt.disposition == "delete"
    assert receipt.error_count == 0
    assert receipt.errors == []
    # The durable receipt is the record of a destructive act; the wire result
    # points at it rather than being the only account of what happened.
    assert Path(receipt.receipt_path).is_file()


def test_archive_disposition_reports_bytes_archived_not_destroyed(
    fake_workspace,
) -> None:
    """``archive`` and ``delete`` must not read the same on the wire."""
    _run(fake_workspace, "expired")
    report = project_workspace_cleanup_report()

    receipt = reclaim_workspace_cleanup_confirmed(
        tier="worktrees",
        disposition="archive",
        confirm_token=report.confirm_token,
    )

    assert receipt.disposition == "archive"
    assert receipt.bytes_archived > 0
    assert receipt.archive_paths
    assert all(Path(path).exists() for path in receipt.archive_paths)


def test_force_requires_an_explicit_cutoff(fake_workspace) -> None:
    """Forcing against a default retention window is refused, as on the CLI.

    ``force`` overrides the engine's value protections; letting it ride on an
    implicit 30-day default would make the most dangerous flag the easiest one
    to pass by accident.
    """
    _run(fake_workspace, "expired")

    with pytest.raises(ValueError, match="explicit older_than_days"):
        project_workspace_cleanup_report(force=True)

    forced = project_workspace_cleanup_report(older_than_days=7, force=True)
    assert forced.force is True
    assert forced.older_than_days == 7


@pytest.mark.parametrize("days", [0, -1])
def test_non_positive_cutoff_is_rejected(fake_workspace, days: int) -> None:
    """A zero or negative window would silently mean 'everything'."""
    with pytest.raises(ValueError, match="positive"):
        project_workspace_cleanup_report(older_than_days=days)


def test_report_forwards_the_cutoff_to_the_engine(fake_workspace, monkeypatch) -> None:
    """The wire cutoff must reach the engine as a real retention window."""
    _run(fake_workspace, "expired", retention_until=_FAR_FUTURE)
    seen: list[dict] = []
    from orcho_mcp.services import workspace_cleanup as module

    original = module.report_workspace_cleanup

    def recorded(**kwargs):
        seen.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(module, "report_workspace_cleanup", recorded)
    project_workspace_cleanup_report(older_than_days=3)

    assert seen[0]["older_than"] == timedelta(days=3)
    assert seen[0]["cwd"] is None
    assert seen[0]["force"] is False
