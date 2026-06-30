"""Worktree-continuity projection surfaced on ``orcho_run_status`` (T4).

orcho-core persists the diff-aware follow-up worktree decision under
``meta['worktree']['followup_continuity']`` (``mode_label`` / ``blocked`` /
``reason`` / ``diff_source``); a non-follow-up run keeps the ``worktree``
block without that sub-block. These tests pin that ``RunStatus`` exposes
the normalised ``subject_mode`` plus ``diff_source`` / ``block_message``
as structured fields — including the core clean-HEAD recovery warning — so
a client never parses logs:

- same-run retained reports a preserved worktree (the provider-fallback
  case);
- the diff-carried "reused parent" mode is visible;
- the artifact-only block surfaces the core warning verbatim.
"""
from __future__ import annotations

from orcho_mcp.tools import orcho_run_status
from tests.fixtures.mcp_workspace import meta, write_run


def _worktree(**extra):
    base = {
        "isolation": "per_run",
        "path": "/p/wt/run",
        "base_ref": "HEAD",
        "branch_ref": "orcho/run/x",
    }
    base.update(extra)
    return base


def test_same_run_retained_reports_preserved_worktree(fake_workspace):
    # No followup_continuity sub-block → the run kept its own worktree.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="running", project="/p/x", task="t",
                  worktree=_worktree()),
    )

    wc = orcho_run_status("20260101_000001").worktree_continuity

    assert wc is not None
    assert wc.has_worktree is True
    assert wc.subject_mode == "same_run_retained"
    assert wc.is_followup_continuity is False
    assert wc.worktree_preserved is True
    assert wc.path == "/p/wt/run"
    assert wc.diff_source is None
    assert wc.blocked is False
    assert wc.block_message is None


def test_reused_parent_diff_carried_mode_visible(fake_workspace):
    write_run(
        fake_workspace, "20260101_000002",
        meta=meta(status="running", project="/p/x", task="t",
                  worktree=_worktree(followup_continuity={
                      "mode_label": "reused parent /p/wt/parent",
                      "blocked": False,
                      "reason": None,
                      "diff_source": "worktree",
                  })),
    )

    wc = orcho_run_status("20260101_000002").worktree_continuity

    assert wc is not None
    assert wc.subject_mode == "reused_parent"
    assert wc.diff_source == "worktree"
    assert wc.is_followup_continuity is True
    assert wc.blocked is False
    assert wc.worktree_preserved is True
    assert wc.mode_label == "reused parent /p/wt/parent"


def test_clean_head_no_undelivered_diff_mode(fake_workspace):
    write_run(
        fake_workspace, "20260101_000003",
        meta=meta(status="running", project="/p/x", task="t",
                  worktree=_worktree(followup_continuity={
                      "mode_label": "clean HEAD (parent had no undelivered diff)",
                      "blocked": False,
                      "reason": None,
                      "diff_source": "none",
                  })),
    )

    wc = orcho_run_status("20260101_000003").worktree_continuity

    assert wc is not None
    assert wc.subject_mode == "clean_head_no_undelivered_diff"
    assert wc.diff_source == "none"
    assert wc.blocked is False
    assert wc.worktree_preserved is True


def test_blocked_artifact_surfaces_core_clean_head_warning(fake_workspace):
    warning = (
        "follow-up parent has an undelivered diff that exists only as a "
        "diff.patch artifact (the parent worktree is clean or absent). "
        "This run does not apply diff artifacts, so it refuses to start "
        "on a clean HEAD and silently drop the parent's change. Recover "
        "by resuming the parent run."
    )
    write_run(
        fake_workspace, "20260101_000004",
        meta=meta(status="halted", project="/p/x", task="t",
                  worktree=_worktree(followup_continuity={
                      "mode_label": "blocked: parent diff/worktree unavailable",
                      "blocked": True,
                      "reason": warning,
                      "diff_source": "artifact",
                  })),
    )

    wc = orcho_run_status("20260101_000004").worktree_continuity

    assert wc is not None
    assert wc.subject_mode == "blocked_parent_diff_unavailable"
    assert wc.diff_source == "artifact"
    assert wc.blocked is True
    assert wc.worktree_preserved is False
    # The core clean-HEAD recovery warning is a structured field, verbatim.
    assert wc.block_message == warning
    assert "clean HEAD" in wc.block_message
    assert "resuming the parent run" in wc.block_message


def test_no_worktree_block_yields_empty_projection(fake_workspace):
    write_run(
        fake_workspace, "20260101_000005",
        meta=meta(status="running", project="/p/x", task="t"),
    )

    wc = orcho_run_status("20260101_000005").worktree_continuity

    assert wc is not None
    assert wc.has_worktree is False
    assert wc.subject_mode is None
    assert wc.worktree_preserved is False
    assert wc.is_followup_continuity is False
