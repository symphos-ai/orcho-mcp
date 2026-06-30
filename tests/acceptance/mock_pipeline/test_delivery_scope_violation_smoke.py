"""Delivery-scope violation E2E smoke through the MCP delivery surface (F2).

Proves the strict-mono ``delivery_scope_violation`` block reaches the MCP wire
from REAL, core-produced state — never a hand-written ``commit_delivery`` dict
and never a monkeypatched SDK return:

1. Build real primary + sibling git repos, register both as workspace project
   aliases in ``.orcho/config.local.json``, and dirty the sibling repo.
2. Drive orcho-core's ``resolve_commit_delivery`` under
   ``delivery_scope='strict_mono'`` — core's T4 enforcement collects the
   sibling change and parks a typed, reversible gate carrying
   ``scope_blocker='delivery_scope_violation'`` and the per-alias disclosure.
3. Persist that core-produced decision as ``meta.commit_delivery`` and read it
   back through the MCP tools ``orcho_delivery_gate`` (projection) and
   ``orcho_delivery_decide`` (a real ``approve`` attempt), asserting the typed
   blocker, per-alias disclosure, and blocked/available actions.

Mandatory (T5 Done Criteria): NO conditional skip. This MUST run against the
current orcho-core (worktree or promoted) that carries the delivery-scope axis;
a core that predates it makes the test FAIL rather than silently skip, so a
green run can never hide a broken delivery-scope surface. Backward-compat
projection wiring against an old core stays covered by the synthetic
unit test ``test_delivery_gate_surfaces_scope_violation``.

Marked ``mcp_integration`` so the default run skips it (by marker selection,
not an in-test skip); opt-in with ``pytest -m mcp_integration``.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.mcp_integration

_RUN_ID = "20260101_000777"


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


@pytest.mark.serial
def test_mcp_delivery_surface_projects_core_produced_strict_scope_block(
    tmp_path, monkeypatch,
):
    # Importing core internals here (not at module top) keeps collection cheap
    # and makes the new-core dependency explicit to this opt-in test.
    from core.io.git_helpers import create_worktree
    from pipeline.engine.commit_delivery import resolve_commit_delivery

    from orcho_mcp.schemas import DeliveryDecideResult, DeliveryGateProjection
    from orcho_mcp.tools import orcho_delivery_decide, orcho_delivery_gate

    ws = tmp_path / "ws"
    primary = tmp_path / "orcho-core"
    sibling = tmp_path / "orcho-mcp"
    _init_repo(primary)
    _init_repo(sibling)

    # Register both repos as workspace aliases (the alias→path map T4's
    # multi-repo collection resolves through), then dirty the sibling.
    cfg_dir = ws / ".orcho"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.local.json").write_text(
        json.dumps({"projects": {
            "orcho-core": str(primary),
            "orcho-mcp": str(sibling),
        }}),
        encoding="utf-8",
    )
    (sibling / "read.py").write_text("strict-mono violation\n", encoding="utf-8")

    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)

    # The run dir MUST live where ``find_run`` resolves it from the workspace,
    # so the MCP tools (cwd=None) discover the same run by id.
    run_dir = ws / "runspace" / "runs" / _RUN_ID
    run_dir.mkdir(parents=True)

    # A run-owned worktree carrying a real primary diff vs baseline.
    baseline = _head(primary)
    result = create_worktree(
        repo=primary,
        base_ref=baseline,
        target_path=run_dir / "checkout",
        branch_name="orcho/run/scope-smoke",
    )
    assert result.ok, result.error
    checkout = run_dir / "checkout"
    (checkout / "app.txt").write_text("base\nrun-owned change\n", encoding="utf-8")

    session = {
        "status": "done",
        "phases": {
            "final_acceptance": {"verdict": "APPROVED", "short_summary": "ok"},
        },
        "auto_detect": {
            "detection_state": "recommended",
            "actual_profile": "feature",
            "actual_mode": "pro",
            "delivery_scope": "strict_mono",
            "delivery_projects": ["orcho-core", "orcho-mcp"],
        },
    }

    # CORE-PRODUCED gate: no synthetic commit_delivery dict. Strict-mono +
    # a real sibling edit parks a typed, reversible scope block (no exception).
    decision = resolve_commit_delivery(
        project_dir=primary,
        source_worktree=checkout,
        run_dir=run_dir,
        run_id=_RUN_ID,
        session=session,
        commit_config={
            "enabled": True,
            "auto_in_ci": "approve",
            "add_untracked": True,
            "default_strategy": "release_summary",
        },
        no_interactive=True,
        baseline_ref=baseline,
    )
    assert decision.scope_blocker == "delivery_scope_violation", (
        "core did not produce a strict-mono scope block — the spawned "
        "orcho-core predates the delivery-scope axis (T4). Run this smoke "
        f"against the current core. decision={decision.to_dict()!r}"
    )

    # Persist exactly what core produced; the MCP surface reads it back.
    (run_dir / "meta.json").write_text(
        json.dumps({
            "status": "done",
            "project": str(primary),
            "commit_delivery": decision.to_dict(),
        }),
        encoding="utf-8",
    )

    # ── MCP projection surface (orcho_delivery_gate) ─────────────────────────
    gate = orcho_delivery_gate(_RUN_ID)
    assert isinstance(gate, DeliveryGateProjection)
    assert gate.scope_blocker == "delivery_scope_violation"
    assert "[orcho-mcp]/read.py" in gate.scope_disclosure
    # Shipping refused; the gate stays reversible (skip / halt remain).
    assert "approve" in gate.blocked_actions
    assert "apply" in gate.blocked_actions
    available = [a.action for a in gate.available_actions]
    assert "skip" in available
    assert "halt" in available

    # ── MCP decision surface (orcho_delivery_decide approve → refused) ───────
    decided = orcho_delivery_decide(_RUN_ID, "approve")
    assert isinstance(decided, DeliveryDecideResult)
    assert decided.accepted is False
    assert decided.blocker == "delivery_scope_violation"
    assert "[orcho-mcp]/read.py" in decided.scope_disclosure
