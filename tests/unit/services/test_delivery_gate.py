"""Delivery / correction gate projection (T2).

``project_delivery_gate`` classifies an Orcho-managed run's post-release
delivery state into one typed :class:`DeliveryGateProjection`. These tests
pin the authority contract:

- ``kind`` and available actions come from orcho-core's
  ``delivery_decision_state`` SDK surface;
- a pending gate with a missing / corrupt *secondary* artifact
  (``commit_decisions`` or ``diff.patch``) degrades the diff summary but is
  never collapsed into ``direct_checkout_or_running``;
- the gate emits one ``ready_call`` to ``orcho_delivery_decide`` per
  SDK-available action.
"""
from __future__ import annotations

from types import SimpleNamespace

from orcho_mcp.services.delivery_gate import project_delivery_gate
from tests.fixtures.mcp_workspace import (
    commit_decision,
    commit_delivery,
    diff_patch_text,
    meta,
    write_run,
)

_RUN = "20260619_000001"


def _action_names(proj) -> list[str]:
    return [a.action for a in proj.available_actions]


def _ready_action_names(proj) -> list[str]:
    return [a.args["action"] for a in proj.next_actions]


def test_project_delivery_gate_calls_sdk_state_with_cwd_none(
    fake_workspace,
    monkeypatch,
):
    calls: list[dict[str, object]] = []

    def fake_state(run_id: str, **kwargs):
        calls.append({"run_id": run_id, "kwargs": kwargs})
        return SimpleNamespace(
            run_id=run_id,
            decidable=False,
            kind="none",
            available_actions=(),
            blocked_actions=(),
            default_action=None,
            reason="no pending delivery gate",
        )

    monkeypatch.setattr(
        "orcho_mcp.services.delivery_gate._sdk_delivery_decision_state",
        fake_state,
    )
    write_run(fake_workspace, _RUN, meta=meta(status="running"))

    project_delivery_gate(_RUN)

    assert calls == [{"run_id": _RUN, "kwargs": {"cwd": None}}]


# (a) approved pending delivery with full secondary artifacts -----------------


def test_approved_pending_delivery_full_artifacts(fake_workspace):
    write_run(
        fake_workspace, _RUN,
        meta=meta(
            status="halted",
            project="/repo/checkout",
            commit_delivery=commit_delivery(
                status="pending",
                action="approve",
                release_verdict="APPROVED",
                changed_paths=["src/a.py", "src/b.py"],
                untracked_paths=["src/new.py"],
                project_path="/repo/checkout",
                source_path="/repo/worktree",
            ),
        ),
        commit_decision=commit_decision(files_staged=["src/a.py", "src/b.py"]),
        diff_patch=diff_patch_text("src/a.py", "src/b.py"),
    )

    proj = project_delivery_gate(_RUN)

    assert proj.kind == "delivery_decision_required"
    assert proj.release == "approved"
    assert proj.target_checkout == "/repo/checkout"
    assert proj.retained_worktree == "/repo/worktree"
    assert proj.diff.degraded is False
    assert proj.diff.files_changed == 2
    assert proj.diff.changed_paths == ["src/a.py", "src/b.py"]
    assert proj.diff.untracked_paths == ["src/new.py"]
    assert proj.default_action == "approve"
    assert _action_names(proj) == ["approve", "apply", "skip", "halt"]
    # creates_commit flag is correct per action.
    by_action = {a.action: a for a in proj.available_actions}
    assert by_action["approve"].creates_commit is True
    assert by_action["apply"].creates_commit is False
    assert all(a.effect for a in proj.available_actions)
    assert proj.blocked_actions == []
    assert _ready_action_names(proj) == ["approve", "apply", "skip", "halt"]
    for na in proj.next_actions:
        assert na.kind == "ready_call"
        assert na.requires_operator_input is False
        assert na.tool == "orcho_delivery_decide"
        assert na.args["run_id"] == _RUN


# (b) rejected correction + fix_requested correction --------------------------


def test_rejected_pending_is_correction(fake_workspace):
    write_run(
        fake_workspace, _RUN,
        meta=meta(
            status="halted",
            commit_delivery=commit_delivery(
                status="pending",
                action="fix",
                release_verdict="REJECTED",
                changed_paths=["src/a.py"],
            ),
        ),
        commit_decision=commit_decision(action="fix"),
        diff_patch=diff_patch_text("src/a.py"),
    )

    proj = project_delivery_gate(_RUN)

    assert proj.kind == "correction_decision_required"
    assert proj.release == "rejected"
    assert proj.diff.degraded is False
    # Core hard-guards shipping actions and skip on a rejected release: a
    # current blocker must be fixed or halted, not silently settled.
    assert _action_names(proj) == ["fix", "halt"]
    assert proj.blocked_actions == ["approve", "apply", "skip"]
    creates = {a.action: a.creates_commit for a in proj.available_actions}
    assert creates == {
        "fix": False, "halt": False,
    }
    assert _ready_action_names(proj) == ["fix", "halt"]


def test_fix_requested_status_is_correction(fake_workspace):
    write_run(
        fake_workspace, _RUN,
        meta=meta(
            status="halted",
            halt_reason="commit_decision_fix",
            commit_delivery=commit_delivery(
                status="fix_requested",
                action="fix",
                release_verdict="REJECTED",
                changed_paths=["src/a.py"],
            ),
        ),
        commit_decision=commit_decision(action="fix", commit_status="fix_requested"),
        diff_patch=diff_patch_text("src/a.py"),
    )

    proj = project_delivery_gate(_RUN)

    assert proj.kind == "correction_decision_required"
    assert proj.diff.degraded is False
    # Correction-followup contract: fix already requested → only ``halt`` remains; the inert ``fix``
    # repeat joins the blocked shipping/skip set and is never offered.
    assert _action_names(proj) == ["halt"]
    assert proj.blocked_actions == ["fix", "approve", "apply", "skip"]
    # The gate surfaces a typed from_run_plan follow-up action (ready_call),
    # ahead of the residual halt decide call.
    starts = [na for na in proj.next_actions if na.tool == "orcho_run_start"]
    assert len(starts) == 1
    assert starts[0].kind == "ready_call"
    assert starts[0].args["from_run_plan"] == _RUN
    # The retained diff path rides as typed, machine-readable ``context`` — never
    # as a tool arg and never only in the (non-contractual) intent prose.
    assert "action" not in starts[0].args
    ctx = starts[0].context or {}
    assert ctx.get("from_run_plan") == _RUN
    assert str(ctx.get("diff_path", "")).endswith("diff.patch")
    # The residual halt decide call is still present.
    assert any(
        na.tool == "orcho_delivery_decide" and na.args.get("action") == "halt"
        for na in proj.next_actions
    )


# (c) terminal status / no commit_delivery -> direct --------------------------


def test_committed_terminal_is_direct(fake_workspace):
    write_run(
        fake_workspace, _RUN,
        meta=meta(
            status="done",
            commit_delivery=commit_delivery(
                status="committed",
                action="approve",
                release_verdict="APPROVED",
                commit_sha="abc123",
            ),
        ),
    )

    proj = project_delivery_gate(_RUN)

    assert proj.kind == "direct_checkout_or_running"
    assert proj.available_actions == []
    assert proj.next_actions == []
    assert proj.message


def test_skipped_terminal_is_direct(fake_workspace):
    write_run(
        fake_workspace, _RUN,
        meta=meta(
            status="halted",
            commit_delivery=commit_delivery(status="skipped", action="skip"),
        ),
    )

    proj = project_delivery_gate(_RUN)
    assert proj.kind == "direct_checkout_or_running"
    assert proj.available_actions == []


def test_halted_terminal_is_direct(fake_workspace):
    write_run(
        fake_workspace, _RUN,
        meta=meta(
            status="halted",
            halt_reason="commit_decision_halt",
            commit_delivery=commit_delivery(status="halted", action="halt"),
        ),
    )

    proj = project_delivery_gate(_RUN)
    assert proj.kind == "direct_checkout_or_running"


def test_no_commit_delivery_is_direct(fake_workspace):
    write_run(
        fake_workspace, _RUN,
        meta=meta(status="running", project="/repo/checkout"),
    )

    proj = project_delivery_gate(_RUN)

    assert proj.kind == "direct_checkout_or_running"
    assert proj.available_actions == []
    assert proj.next_actions == []
    assert "direct checkout" in proj.message


# (d) pending meta + MISSING commit_decisions -> kind kept, degraded ----------


def test_pending_missing_commit_decisions_degrades_not_hides(fake_workspace):
    write_run(
        fake_workspace, _RUN,
        meta=meta(
            status="halted",
            commit_delivery=commit_delivery(
                status="pending",
                release_verdict="APPROVED",
                changed_paths=["src/a.py", "src/b.py"],
            ),
        ),
        # No commit_decision artifact; diff.patch present and valid.
        diff_patch=diff_patch_text("src/a.py", "src/b.py"),
    )

    proj = project_delivery_gate(_RUN)

    # Kind preserved despite the missing secondary artifact.
    assert proj.kind == "delivery_decision_required"
    assert proj.diff.degraded is True
    # changed_paths still come from the authoritative meta.
    assert proj.diff.changed_paths == ["src/a.py", "src/b.py"]
    assert proj.diff.files_changed == 2
    assert "commit_decisions" in proj.message


# (e) pending meta + CORRUPT diff.patch -> kind kept, diff from meta ----------


def test_pending_corrupt_diff_patch_degrades_not_hides(fake_workspace):
    write_run(
        fake_workspace, _RUN,
        meta=meta(
            status="halted",
            commit_delivery=commit_delivery(
                status="pending",
                release_verdict="APPROVED",
                changed_paths=["src/a.py"],
            ),
        ),
        commit_decision=commit_decision(files_staged=["src/a.py"]),
        # Non-empty body with no recognizable diff structure → corrupt.
        diff_patch="this is not a valid unified diff \x00\x01 garbage",
    )

    proj = project_delivery_gate(_RUN)

    assert proj.kind == "delivery_decision_required"
    assert proj.diff.degraded is True
    # Diff summary falls back to meta-recorded paths.
    assert proj.diff.changed_paths == ["src/a.py"]
    assert proj.diff.files_changed == 1
    assert "diff.patch" in proj.message


# (f) fully missing / corrupt meta -> direct with message ---------------------


def test_missing_meta_file_is_direct(fake_workspace):
    # Run dir exists but no meta.json written.
    write_run(fake_workspace, _RUN)

    proj = project_delivery_gate(_RUN)

    assert proj.kind == "direct_checkout_or_running"
    assert proj.available_actions == []
    assert proj.message


def test_corrupt_meta_is_direct(fake_workspace):
    write_run(fake_workspace, _RUN, meta_text="{ not valid json ::::")

    proj = project_delivery_gate(_RUN)

    assert proj.kind == "direct_checkout_or_running"
    assert proj.next_actions == []
    assert proj.message


# status key authority --------------------------------------------------------


def test_status_read_from_status_key_not_commit_status(fake_workspace):
    # The decision dict carries the authoritative ``status`` AND a stale
    # ``commit_status`` alias with a different value. Classification must read
    # ``status`` (pending), not the alias (committed).
    cd = commit_delivery(
        status="pending", release_verdict="APPROVED", changed_paths=["src/a.py"],
    )
    cd["commit_status"] = "committed"
    write_run(
        fake_workspace, _RUN,
        meta=meta(status="halted", commit_delivery=cd),
        commit_decision=commit_decision(),
        diff_patch=diff_patch_text("src/a.py"),
    )

    proj = project_delivery_gate(_RUN)

    assert proj.kind == "delivery_decision_required"


def test_commit_status_alias_without_status_is_not_decidable(fake_workspace):
    # No authoritative ``status``; the audit-artifact-style ``commit_status``
    # alias is not enough to make the SDK state decidable.
    cd = commit_delivery(
        status="pending", release_verdict="APPROVED", changed_paths=["src/a.py"],
    )
    del cd["status"]
    cd["commit_status"] = "pending"
    write_run(
        fake_workspace, _RUN,
        meta=meta(status="halted", commit_delivery=cd),
        commit_decision=commit_decision(),
        diff_patch=diff_patch_text("src/a.py"),
    )

    proj = project_delivery_gate(_RUN)

    assert proj.kind == "direct_checkout_or_running"
    assert proj.available_actions == []
    assert proj.next_actions == []


# (T5) delivery-scope violation: scope blocker + per-alias disclosure ---------


def test_delivery_gate_surfaces_scope_violation(fake_workspace, monkeypatch):
    # Mock E2E (T5): when core's delivery_decision_state reports a strict-mono
    # scope violation (T4), the gate projection must surface the typed
    # ``scope_blocker`` and the per-alias sibling disclosure, with shipping
    # actions blocked and skip/halt still available. The SDK boundary is
    # monkeypatched to the new-core shape so the test pins MCP projection
    # wiring independent of the installed core version.
    def fake_state(run_id: str, **kwargs):
        return SimpleNamespace(
            run_id=run_id,
            decidable=True,
            kind="delivery",
            available_actions=("skip", "halt"),
            blocked_actions=("approve", "apply"),
            default_action="skip",
            reason=(
                "delivery_scope_violation — sibling-repo changes outside "
                "strict mono scope; expand the delivery scope or skip / halt"
            ),
            scope_disclosure=("[orcho-mcp]/read.py",),
        )

    monkeypatch.setattr(
        "orcho_mcp.services.delivery_gate._sdk_delivery_decision_state",
        fake_state,
    )
    write_run(
        fake_workspace, _RUN,
        meta=meta(
            status="done",
            project="/repo/checkout",
            commit_delivery=commit_delivery(
                status="pending",
                action="none",
                release_verdict="APPROVED",
                project_path="/repo/checkout",
                source_path="/repo/worktree",
            ),
        ),
    )

    proj = project_delivery_gate(_RUN)

    assert proj.scope_blocker == "delivery_scope_violation"
    assert proj.scope_disclosure == ["[orcho-mcp]/read.py"]
    assert "approve" in proj.blocked_actions
    assert "apply" in proj.blocked_actions
    assert _action_names(proj) == ["skip", "halt"]


def test_delivery_gate_no_scope_block_has_empty_scope_fields(fake_workspace):
    # A normal approved pending gate (real stable-core SDK state, no scope
    # dimension) leaves scope_blocker/scope_disclosure at their empty defaults.
    write_run(
        fake_workspace, _RUN,
        meta=meta(
            status="halted",
            project="/repo/checkout",
            commit_delivery=commit_delivery(
                status="pending", action="approve", release_verdict="APPROVED",
                project_path="/repo/checkout", source_path="/repo/worktree",
            ),
        ),
        commit_decision=commit_decision(files_staged=["src/a.py"]),
        diff_patch=diff_patch_text("src/a.py"),
    )

    proj = project_delivery_gate(_RUN)

    assert proj.scope_blocker is None
    assert proj.scope_disclosure == []


# ── Correction-followup contract: superseded parent has no pending gate, no stale blockers ────────


def test_superseded_parent_is_direct_with_superseded_message(fake_workspace):
    # A rejected-FA parent closed by a successful from_run_plan follow-up: core
    # finalization (T2) evicted the phantom commit_delivery and stamped
    # superseded_by_followup. The gate is closed (direct), no actions, and the
    # message names the superseding child — not the parent's old blockers.
    write_run(
        fake_workspace, _RUN,
        meta=meta(
            status="done",
            project="/repo/checkout",
            superseded_by_followup={
                "child_run_id": "20260619_000999",
                "child_status": "done",
                "delivery_status": "committed",
                "reason": "correction delivered via from_run_plan follow-up",
            },
            phases={
                "final_acceptance": {
                    "verdict": "REJECTED",
                    "release_blockers": [{"id": "RB1", "detail": "data loss"}],
                },
            },
        ),
    )

    proj = project_delivery_gate(_RUN)

    assert proj.kind == "direct_checkout_or_running"
    assert proj.available_actions == []
    assert proj.blocked_actions == []
    assert proj.next_actions == []
    assert proj.message
    assert "superseded" in proj.message
    assert "20260619_000999" in proj.message
