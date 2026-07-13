"""Best-effort mock-pipeline smoke for ``orcho_delivery_gate`` (T5).

Spawns a real ``--mock`` pipeline subprocess through the supervisor, waits
for it to finish, then calls ``orcho_delivery_gate`` against the completed
run and asserts the tool returns a *typed* ``DeliveryGateProjection`` whose
``kind`` is one of the published literals.

Expected outcome under mock mode: a hermetic mock run does not park at an
Orcho post-release delivery gate (it either commits straight through or
never reaches a pending commit-delivery decision), so the authoritative
``meta`` commit-delivery status is terminal / absent and the projection is
``direct_checkout_or_running``. That is recorded here as the CORRECT result,
not a gap — it proves the tool distinguishes a plain completed run from an
Orcho-managed delivery gate end-to-end through the wire.

If a future mock profile does reach a delivery gate, the smoke also accepts
``delivery_decision_required`` and pins the gate invariant: MCP exposes typed
ready calls that delegate the decision to core through ``orcho_delivery_decide``.

Coverage note (deferred delivery-gate case): orcho-core has no
non-interactive mock profile that reliably PARKS a run at a pending delivery
gate, so the ``delivery_decision_required`` path cannot be driven from a mock
run here. That case is covered instead by the L1 unit tests in
``tests/unit/services/test_delivery_gate.py`` and the L3 stdio smoke
``test_stdio_delivery_gate_correction_call_tool`` against synthetic runs.

Marked ``mcp_integration`` so the default test run skips it (``pyproject``
addopts pin ``-m 'not mcp_integration'`` to keep the broad suite fast). It is
therefore *opt-in only*: a bare ``pytest <thisfile>`` reports ``deselected`` /
exit 5, which is the gate working as designed, not a failure.

Accepted verification command (executes the smoke, never deselects)::

    python -m pytest -q -m mcp_integration \
        tests/acceptance/mock_pipeline/test_delivery_gate_smoke.py

or, equivalently, the recorded ``make test-delivery-gate-smoke`` target. The
trailing ``-m mcp_integration`` overrides the addopts marker filter — the same
opt-in pattern every sibling smoke in this directory uses.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.mcp_integration

_VALID_KINDS = {
    "delivery_decision_required",
    "correction_decision_required",
    "delivery_completed",
    "direct_checkout_or_running",
}


@pytest.fixture
def mock_project(tmp_path, monkeypatch):
    """Minimal git-backed project + workspace for a mock run.

    Project dir must be a real git repo — orcho-core's worktree resolver
    hard-fails on a non-git ``project_dir``.
    """
    from tests.conftest import init_git_repo

    ws = tmp_path / "ws"
    project = ws / "demo_project"
    runs_dir = ws / "runspace" / "runs"
    project.mkdir(parents=True)
    runs_dir.mkdir(parents=True)
    (project / "README.md").write_text("# Demo project\n", encoding="utf-8")
    init_git_repo(project)
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    return project


@pytest.mark.asyncio
async def test_orcho_delivery_gate_typed_kind_after_mock_run(mock_project):
    """A completed ``--mock`` run yields a typed delivery-gate projection.

    Drives the supervisor's spawn → reap path, then calls the read-only
    ``orcho_delivery_gate`` tool and asserts a typed ``kind``. A hermetic
    mock run is expected to classify as ``direct_checkout_or_running`` (no
    pending Orcho delivery gate); that is the documented correct outcome.
    """
    from orcho_mcp.schemas import DeliveryGateProjection
    from orcho_mcp.supervisor import RunsSupervisor
    from orcho_mcp.tools import orcho_delivery_gate

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="trivial mock task — delivery gate smoke",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if handle.popen is not None and handle.popen.poll() is not None:
            break
        await asyncio.sleep(0.5)
    else:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.kill()
        pytest.skip(
            "mock pipeline did not finish within 90s — delivery-gate smoke "
            "deferred (mock environment unavailable / too slow)",
        )

    # Let the reap task settle the terminal status.
    for _ in range(50):
        if handle.status != "running":
            break
        await asyncio.sleep(0.1)

    proj = orcho_delivery_gate(handle.run_id)

    # Typed wire model end-to-end (not a dict / not raw prose).
    assert isinstance(proj, DeliveryGateProjection)
    assert proj.run_id == handle.run_id
    assert proj.kind in _VALID_KINDS, f"unexpected gate kind: {proj.kind!r}"

    if proj.kind in ("direct_checkout_or_running", "delivery_completed"):
        # Both are terminal for a hermetic mock run: no approve/apply/fix is
        # offered and there are no next actions. ``delivery_completed`` would
        # additionally carry the landed-delivery facts, but a hermetic mock
        # normally classifies as ``direct_checkout_or_running`` (no Orcho gate).
        assert proj.available_actions == []
        assert proj.next_actions == []
        assert proj.message
    else:
        # If a mock profile ever reaches a real gate, pin the invariant:
        # every action MCP exposes is a typed delegation to core's decision API.
        assert proj.kind in {
            "delivery_decision_required",
            "correction_decision_required",
        }
        assert proj.available_actions, "a gate must offer operator actions"
        assert proj.next_actions, "a gate must surface a next action"
        for na in proj.next_actions:
            assert na.kind == "ready_call"
            assert na.requires_operator_input is False
            assert na.tool == "orcho_delivery_decide"
            assert na.args.get("run_id") == handle.run_id
            assert na.args.get("action") in proj.available_actions


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── correction-followup contract end-to-end ──────────────────────────────────
#
# orcho-core has no non-interactive mock profile that reliably PARKS a run at a
# rejected delivery gate or rejects final_acceptance (documented above), and the
# cross-run supersede needs a real from_run_plan child to deliver — multi-run
# orchestration the single-spawn mock harness cannot drive. So, mirroring the
# existing precedent (the parking case is covered against synthetic runs), this
# scenario smoke drives the FULL contract through the actual MCP/core state
# transition functions: MCP ``orcho_delivery_decide(action='fix')`` marks the
# rejected parent, then the same core finalization helper used at follow-up child
# shutdown supersedes the parent. No test code rewrites the parent into a closed
# state by hand.


def _rejected_final_acceptance_meta(
    parent_run_id: str,
    *,
    project_path: str,
    source_path: str,
):
    from tests.fixtures.mcp_workspace import meta

    return meta(
        status="halted",
        project=project_path,
        halt_reason="final_acceptance_rejected",
        halt={"reason": "final_acceptance_rejected", "phase": "final_acceptance"},
        rejected_outcome={
            "phase": "final_acceptance",
            "reason": "final_acceptance_rejected",
            "status": "halted",
            "release_verdict": "REJECTED",
            "release_blockers": [{"id": "RB1", "detail": "data loss"}],
        },
        commit_delivery={
            "run_id": parent_run_id,
            "status": "not_applicable",
            "action": "none",
            "release_verdict": "REJECTED",
            "project_path": project_path,
            "source_path": source_path,
            "baseline_ref": "HEAD",
            "changed_paths": ["src/a.py"],
            "untracked_paths": [],
            "release_blockers": [{"id": "RB1", "detail": "data loss"}],
        },
    )


def _seed_dirty_git_project(path):
    from tests.conftest import init_git_repo

    init_git_repo(path)
    (path / "src").mkdir()
    tracked = path / "src" / "a.py"
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/a.py"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add tracked file"],
        cwd=path,
        check=True,
    )
    tracked.write_text("base\nfixed\n", encoding="utf-8")


def _followup_child_run(workspace, *, parent_run_id: str, child_run_id: str):
    child_dir = workspace / "runspace" / "runs" / child_run_id
    child_dir.mkdir(parents=True)
    return SimpleNamespace(
        output_dir=child_dir,
        state=SimpleNamespace(extras={"plan_source_run_id": parent_run_id}),
        session={"status": "done", "commit_delivery": {"status": "committed"}},
        session_ts=child_run_id,
    )


def test_correction_followup_then_supersede_scenario(fake_workspace):
    """rejected-FA → fix → from_run_plan follow-up → supersede, end-to-end.

    Asserts all four surfaces stay consistent across the two transitions:
    after fix, diagnose + delivery_gate emit the typed from_run_plan action and
    live_status reports resume inert; after supersede, the parent reads
    closed/superseded everywhere with no active correction and no authoritative
    old blockers.
    """
    from pipeline.project.finalization import (
        _supersede_parent_correction_after_followup,
    )

    from orcho_mcp.inspection.diagnosis import inspect_run_diagnosis
    from orcho_mcp.observe.live_status import build_run_live_status
    from orcho_mcp.services.delivery_gate import project_delivery_gate
    from orcho_mcp.services.run_projection import project_run_diagnosis
    from orcho_mcp.tools import orcho_delivery_decide
    from tests.fixtures.mcp_workspace import write_run

    parent = "20260101_000001"
    child = "20260101_000002"
    project = fake_workspace / "repo"
    _seed_dirty_git_project(project)

    write_run(
        fake_workspace,
        parent,
        meta=_rejected_final_acceptance_meta(
            parent,
            project_path=str(project),
            source_path=str(project),
        ),
        diff_patch="diff --git a/src/a.py b/src/a.py\n",
    )

    # ── transition 1: MCP operator decision marks the correction gate ────────
    decided = orcho_delivery_decide(
        parent, "fix", note="operator requested a correction follow-up",
    )
    assert decided.accepted is True
    assert decided.status == "fix_requested"
    assert decided.halt_reason == "commit_decision_fix"
    persisted = json.loads(
        (fake_workspace / "runspace" / "runs" / parent / "meta.json").read_text(
            encoding="utf-8",
        ),
    )
    assert persisted["halt_reason"] == "commit_decision_fix"
    assert persisted["commit_delivery"]["status"] == "fix_requested"

    gate = project_delivery_gate(parent)
    assert gate.kind == "correction_decision_required"
    assert [a.action for a in gate.available_actions] == ["halt"]
    gate_starts = [na for na in gate.next_actions if na.tool == "orcho_run_start"]
    assert len(gate_starts) == 1
    assert gate_starts[0].args["from_run_plan"] == parent
    # The retained diff path rides as typed, machine-readable ``context`` — not
    # only as (non-contractual) intent prose.
    gate_ctx = gate_starts[0].context or {}
    assert gate_ctx.get("from_run_plan") == parent
    assert str(gate_ctx.get("diff_path", "")).endswith("diff.patch")

    diag = project_run_diagnosis(parent)
    assert diag.condition == "correction_followup_required"
    assert diag.recommended_next_action == "start_followup"
    assert diag.followup_diff_path is not None
    # The diagnose wire emits the same typed action with the same machine-readable
    # context, so a typed client gets the diff pointer from both surfaces.
    diag_starts = [
        na for na in inspect_run_diagnosis(parent).next_actions
        if na.tool == "orcho_run_start"
    ]
    assert len(diag_starts) == 1
    diag_ctx = diag_starts[0].context or {}
    assert diag_ctx.get("from_run_plan") == parent
    assert str(diag_ctx.get("diff_path", "")).endswith("diff.patch")

    card = build_run_live_status(parent)
    assert card.terminal is not None
    assert card.terminal.resume_meaningful is False

    # ── transition 2: from_run_plan child delivered → parent superseded ─────
    _supersede_parent_correction_after_followup(
        _followup_child_run(fake_workspace, parent_run_id=parent, child_run_id=child),
    )

    gate2 = project_delivery_gate(parent)
    assert gate2.kind == "direct_checkout_or_running"
    assert gate2.available_actions == []
    assert "superseded" in gate2.message and child in gate2.message

    diag2 = project_run_diagnosis(parent)
    # Distinct typed closed/superseded state — never a generic inert terminal.
    assert diag2.condition == "closed_by_followup"
    assert diag2.recommended_run_id == child
    assert "superseded" in diag2.reason
    assert diag2.delivery_gate_kind is None
    # The diagnose wire surfaces the typed closed condition and points inspection
    # at the superseding child — no from_run_plan / resume of the closed parent.
    wire2 = inspect_run_diagnosis(parent)
    assert wire2.condition == "closed_by_followup"
    assert wire2.recommended_run_id == child
    assert all(na.tool != "orcho_run_start" for na in wire2.next_actions)
    assert all(na.tool != "orcho_run_resume" for na in wire2.next_actions)

    card2 = build_run_live_status(parent)
    assert card2.state_class == "terminal_success"
    assert card2.consistency_flags == []
    assert card2.terminal is not None
    assert card2.terminal.resume_meaningful is False
    assert "superseded" in card2.next_action


def test_committed_published_delivery_projects_completed_end_to_end(fake_workspace):
    """A committed, published Orcho delivery reads as ``delivery_completed``
    consistently across the gate, evidence, and live-status surfaces.

    Seeds a terminal run whose Orcho-managed delivery already landed
    (``commit_delivery.status == 'committed'``) and opened a pull request
    (``pr_url`` + ``delivery_notices``). Asserts all three read-only surfaces
    agree on the terminal disposition without a second meta read or prose
    scraping: the gate is the terminal ``delivery_completed`` kind carrying the
    published PR facts (and suppressing the stale ``suggested_command``); the
    evidence delivery record carries the same ``pr_url`` / ``delivery_notices``;
    and the live terminal card routes ``terminal_success`` at the PR.
    """
    from orcho_mcp.inspection.evidence import inspect_run_evidence
    from orcho_mcp.observe.live_status import build_run_live_status
    from orcho_mcp.services.delivery_gate import project_delivery_gate
    from tests.fixtures.mcp_workspace import commit_delivery, meta, write_run

    run_id = "20260101_000042"
    pr_url = "https://example.test/pr/42"
    notices = ["PR opened: https://example.test/pr/42"]

    write_run(
        fake_workspace, run_id,
        meta=meta(
            status="done",
            project="/repo/checkout",
            commit_delivery=commit_delivery(
                status="committed",
                action="approve",
                release_verdict="APPROVED",
                project_path="/repo/checkout",
                source_path="/repo/worktree",
                commit_sha="abc123",
                delivery_branch="orcho/deliver/20260101-slug",
                pr_url=pr_url,
                delivery_notices=notices,
                pr_intent={
                    "branch": "orcho/deliver/20260101-slug",
                    "base": "main",
                    "title": "Deliver widget",
                    "suggested_command": "gh pr create --fill",
                },
            ),
        ),
    )

    # ── gate surface — terminal completed kind with published PR facts ───────
    gate = project_delivery_gate(run_id)
    assert gate.kind == "delivery_completed"
    assert gate.available_actions == []
    assert gate.next_actions == []
    assert gate.published is True
    assert gate.pr_url == pr_url
    assert gate.delivery_notices == notices
    assert gate.delivery_branch == "orcho/deliver/20260101-slug"
    # Stale "open a PR" command dropped once the PR is open; the live link is
    # pr_url — the rest of the intent is preserved.
    assert gate.pr_intent is not None
    assert gate.pr_intent.suggested_command is None
    assert gate.pr_intent.branch == "orcho/deliver/20260101-slug"
    assert pr_url in gate.message

    # ── evidence surface — same published facts via the shared helpers ───────
    evidence = inspect_run_evidence(run_id, slice="delivery")
    assert evidence.delivery is not None
    assert evidence.delivery.committed is True
    assert evidence.delivery.pr_url == pr_url
    assert evidence.delivery.delivery_notices == notices

    # ── live-status surface — terminal card carries the disposition ──────────
    card = build_run_live_status(run_id)
    assert card.state_class == "terminal_success"
    assert card.terminal is not None
    assert card.terminal.delivery_committed is True
    assert card.terminal.delivery_published is True
    assert card.terminal.delivery_pr_url == pr_url
    # terminal_success routes a delivered run at its PR, not a manual commit.
    assert pr_url in card.next_action
