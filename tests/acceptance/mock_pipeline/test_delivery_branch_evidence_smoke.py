"""Delivery-branch (ADR 0119) E2E smoke through the MCP delivery surface (T6).

Proves the ``delivery_branch`` + typed ``pr_intent`` facts reach the MCP wire
from REAL, core-produced state — never a hand-written ``commit_delivery`` dict
and never a monkeypatched SDK return. Modelled on
``test_delivery_scope_violation_smoke.py`` (the accepted «core-produced delivery
fact → MCP projection» pattern: a real git repo + the real core engine
functions + ``decision.to_dict()`` persisted as meta + read back through the MCP
tools; it does not spawn a pipeline just to obtain delivery state).

Driven policy — ``protect_default`` / in-place on HEAD=default:

1. A hermetic git repo (``git init -b main``, ``commit.gpgsign=false``, one
   initial commit) with an uncommitted run-owned edit, delivered *in place*
   (``source_worktree == project_dir``) while HEAD is the default branch.
2. The real core engine ``resolve_commit_delivery(...)`` (action ``approve``,
   APPROVED release) then ``apply_commit_delivery(...)`` under an explicit
   ``commit_config={'branch_policy': 'protect_default'}``. On an in-place run
   whose HEAD is the default branch, the policy resolves to the
   ``commit_on_branch`` plan: core creates ``orcho/deliver/<run_id>-<slug>`` and
   commits onto it (never onto ``main``), so a real ``commit_sha`` IS produced
   alongside the ``delivery_branch`` and durable ``pr_intent`` — nothing pushed
   to a remote (hermetic).

Stop-condition (deferred blocker, NOT form-only green): if the spawned
orcho-core predates the ADR 0119 delivery-branch policy — its
``apply_commit_delivery`` takes no ``commit_config`` or its
``decision.to_dict()`` carries no ``delivery_branch`` — this smoke records an
explicit ``pytest.skip`` naming the reason, so the L4 value case is deferred
rather than passing on a form-only assertion. Backward-compat projection wiring
against an old core stays covered by the L1 unit tests in
``tests/unit/inspection/test_evidence.py`` and
``tests/unit/services/test_delivery_gate.py``.

Marked ``mcp_integration`` so the default run skips it (by marker selection, not
an in-test skip); opt-in with ``pytest -m mcp_integration``. A bare
``pytest <thisfile>`` reports ``deselected`` / exit 5 — the gate working as
designed. Accepted verification command::

    python -m pytest -q -m mcp_integration \
        tests/acceptance/mock_pipeline/test_delivery_branch_evidence_smoke.py
"""
from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.mcp_integration

_RUN_ID = "20260101_000119"


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@orcho.invalid"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Orcho Test"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True,
    )
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _head(repo: Path) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _current_branch(repo: Path) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


@pytest.mark.serial
def test_mcp_delivery_surface_projects_core_produced_branch_policy(
    tmp_path, monkeypatch,
):
    # Import core internals inside the test (not at module top) to keep
    # collection cheap and make the new-core dependency explicit to this
    # opt-in smoke.
    from pipeline.engine.commit_delivery import (
        apply_commit_delivery,
        resolve_commit_delivery,
    )

    from orcho_mcp.schemas import DeliveryGateProjection
    from orcho_mcp.schemas.inspection import EvidenceResult, PrIntentRecord
    from orcho_mcp.tools import orcho_delivery_gate, orcho_run_evidence

    # ── Stop-condition A: old core whose apply has no branch-policy hook. ─────
    if "commit_config" not in inspect.signature(apply_commit_delivery).parameters:
        pytest.skip(
            "spawned orcho-core предшествует ADR 0119 delivery-branch policy "
            "(apply_commit_delivery has no commit_config parameter) — соберите/"
            "установите core с branch_policy; запускать против ADR-0119 core",
        )

    ws = tmp_path / "ws"
    repo = tmp_path / "demo_project"
    _init_repo(repo)
    baseline = _head(repo)

    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)

    # The run dir MUST live where ``find_run`` resolves it from the workspace so
    # the MCP tools (cwd=None) discover the same run by id.
    run_dir = ws / "runspace" / "runs" / _RUN_ID
    run_dir.mkdir(parents=True)

    # In-place delivery: the run-owned change is an uncommitted edit in the
    # repo itself, delivered while HEAD is still the default branch ``main``.
    assert _current_branch(repo) == "main"
    (repo / "app.txt").write_text("base\nrun-owned change\n", encoding="utf-8")

    session = {
        "status": "done",
        "phases": {
            "final_acceptance": {
                "verdict": "APPROVED",
                "short_summary": "add widget line",
            },
        },
    }
    commit_config = {
        "enabled": True,
        "auto_in_ci": "approve",
        "add_untracked": True,
        "default_strategy": "release_summary",
        # ADR 0119 — explicit branch policy driving the value path.
        "branch_policy": "protect_default",
    }

    # CORE-PRODUCED decision — no synthetic commit_delivery dict. Resolve then
    # apply the real engine so the delivery branch + commit + pr_intent are all
    # produced by core.
    resolved = resolve_commit_delivery(
        project_dir=repo,
        source_worktree=repo,
        run_dir=run_dir,
        run_id=_RUN_ID,
        session=session,
        commit_config=commit_config,
        no_interactive=True,
        baseline_ref=baseline,
    )
    assert resolved.action == "approve", (
        "release gate did not resolve an approve delivery — setup error, not "
        f"an old core. decision={resolved.to_dict()!r}"
    )

    applied = apply_commit_delivery(
        resolved,
        run_dir=run_dir,
        commit_config=commit_config,
        no_interactive=True,
    )
    cd = applied.to_dict()

    # ── Stop-condition B: old core produced no delivery_branch fact. ─────────
    if not cd.get("delivery_branch"):
        pytest.skip(
            "spawned orcho-core предшествует ADR 0119 delivery-branch policy "
            "(core-produced decision.to_dict() carries no 'delivery_branch') — "
            "соберите/установите core с branch_policy; запускать против "
            f"ADR-0119 core. decision={cd!r}",
        )

    # The commit must have landed on the dedicated delivery branch, never on the
    # protected default branch.
    core_branch = cd["delivery_branch"]
    assert core_branch.startswith("orcho/deliver/"), core_branch
    assert cd.get("status") == "committed", cd
    assert cd.get("commit_sha"), (
        "protect_default commit-on-branch must produce a real commit_sha "
        f"(no fabrication upstream). decision={cd!r}"
    )

    # Persist exactly what core produced; the MCP surface reads it back.
    (run_dir / "meta.json").write_text(
        json.dumps({
            "status": "done",
            "project": str(repo),
            "commit_delivery": cd,
        }),
        encoding="utf-8",
    )

    # ── MCP read-only evidence surface (orcho_run_evidence slice='delivery') ──
    result = orcho_run_evidence(_RUN_ID, slice="delivery")
    assert isinstance(result, EvidenceResult)
    d = result.delivery
    assert d is not None
    # VALUE assert (not form-only): the projected branch equals the core branch.
    assert d.delivery_branch == core_branch
    assert d.delivery_branch  # non-empty
    # Typed pr_intent with non-empty branch / base (title / suggested_command
    # asserted when core emitted them).
    assert isinstance(d.pr_intent, PrIntentRecord)
    assert d.pr_intent.branch
    assert d.pr_intent.base
    if cd.get("pr_intent", {}).get("title"):
        assert d.pr_intent.title == cd["pr_intent"]["title"]
    if cd.get("pr_intent", {}).get("suggested_command"):
        assert d.pr_intent.suggested_command == cd["pr_intent"]["suggested_command"]
    # protect_default commit-on-branch produces a real commit (no fabrication).
    assert d.commit_sha == cd["commit_sha"]
    assert d.committed is True

    # ── MCP projection surface (orcho_delivery_gate) carries the same facts ──
    gate = orcho_delivery_gate(_RUN_ID)
    assert isinstance(gate, DeliveryGateProjection)
    assert gate.delivery_branch == core_branch
    assert isinstance(gate.pr_intent, PrIntentRecord)
    assert gate.pr_intent.branch == d.pr_intent.branch
    assert gate.pr_intent.base == d.pr_intent.base

    # ── slice='all' stays whole: the delivery sub-record is populated. ───────
    all_result = orcho_run_evidence(_RUN_ID, slice="all")
    assert all_result.delivery is not None
    assert all_result.delivery.delivery_branch == core_branch
    assert all_result.delivery.pr_intent is not None
    assert all_result.delivery.pr_intent.branch == d.pr_intent.branch
