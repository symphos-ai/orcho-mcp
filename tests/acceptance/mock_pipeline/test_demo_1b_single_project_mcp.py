"""DEMO-1B — single-project MCP proof loop.

Pins one runnable narrative on top of the MCP read/write surface:

  ACT      orcho_run_start under the feature profile (declares
           human_feedback_on_reject on validate_plan); the mock
           rejection counter drives the plan loop to its final
           rejected round so the handoff fires
  OBSERVE  poll until awaiting_phase_handoff
  INSPECT  orcho_run_evidence(slice="findings", phases=["validate_plan"])
           + read meta.phase_handoff to obtain the canonical handoff_id
  DECIDE   orcho_phase_handoff_decide(..., action="continue")
  ACT      orcho_run_resume(profile="task")   # internal scoped profile:
           # resume skipping plan re-execution on the checkpoint
  OBSERVE  poll until terminal == done
  INSPECT  orcho_run_evidence(slice="all") + metrics + history

This is a proof gate, not a feature: every individual tool is already
covered by its own L4 test. What's pinned here is the chain — a single
pass through the public surface drives a single-project mock pipeline
from start to verified done without raw log scraping, without inventing
new tools, and without changing the wire schema.

Marked ``mcp_integration`` so the default suite stays fast.
"""
from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.mcp_integration


# ── fixture: real disposable copy of orcho-core/examples/golden-api ────────


@pytest.fixture
def golden_project(tmp_path: Path, monkeypatch) -> Path:
    """Disposable copy of the golden-api fixture from orcho-core.

    Why a real fixture instead of conftest's synthetic ``mock_project``:
    DEMO-1B's proof bar is ``terminal == done`` after approve+resume.
    The mock pipeline's BUILD/REVIEW/FIX/final_acceptance agents need a project
    layout with at least ``pyproject.toml`` plus an ``app/`` source
    tree to round-trip through profile=task without falling into
    ``failed``. The synthetic three-file project under
    ``mock_project`` is sufficient for individual-tool L4 contracts but
    not for the integrated proof-loop assertion.

    Copies into ``tmp_path`` so the source fixture is never mutated
    (mock build writes inside the project tree). ``ORCHO_WORKSPACE`` is
    pinned to the tmp workspace via ``monkeypatch``.
    """
    # tests/acceptance/mock_pipeline/<file> → parents: [0]=mock_pipeline,
    # [1]=acceptance, [2]=tests, [3]=orcho-mcp, [4]=workspace-root.
    core_root = Path(__file__).resolve().parents[4] / "orcho-core"
    src = core_root / "examples" / "golden-api"
    if not src.is_dir():
        pytest.skip(f"golden-api fixture not available at {src}")

    ws = tmp_path / "ws"
    project = ws / "demo_project"
    runs_dir = ws / "runspace" / "runs"
    runs_dir.mkdir(parents=True)
    shutil.copytree(src, project)
    # The copied golden-api tree is not a git repo — orcho-core's
    # worktree resolver hard-fails on non-git ``project_dir``
    # (``3b516ec``). Initialise it so the copied files land in a clean
    # git HEAD.
    from tests.conftest import init_git_repo
    init_git_repo(project)
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    return project


# ── helpers (local copies — mirrors style of sibling L4 tests) ──────────────


async def _wait_status(
    run_id: str,
    expected: set[str],
    timeout_s: float = 60.0,
) -> str:
    """Poll ``orcho_run_status`` until ``meta.status`` is in ``expected``."""
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


# ── proof loop ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_demo_1b_single_project_mcp_proof_loop(
    golden_project: Path,
) -> None:
    """DEMO-1B: pause → inspect findings → approve → resume → final inspect.

    Proves an MCP-aware client can drive the full single-project loop
    through the public tool surface only — no raw log scraping, no new
    tools, no schema changes. The matching CLI counterpart is DEMO-1A
    in orcho-core.
    """
    from orcho_mcp.tools import (
        orcho_phase_handoff_decide,
        orcho_run_evidence,
        orcho_run_history,
        orcho_run_metrics,
        orcho_run_resume,
        orcho_run_start,
        orcho_run_status,
    )

    # 1. ACT — spawn a mock run under the feature profile. ``feature``
    # declares ``human_feedback_on_reject`` on ``validate_plan`` with a
    # plan-loop ``max_rounds=2``, so ``mock_validate_plan_reject=3``
    # forces every plan round to reject and the pause fires on the
    # final round.
    started = await orcho_run_start(
        task="DEMO-1B: prove single-project MCP proof loop",
        project_dir=str(golden_project),
        profile="feature",
        mock=True,
        max_rounds=1,
        mock_validate_plan_reject=3,
    )
    assert started.run_id

    # 2. OBSERVE — wait for the handoff to fire.
    paused = await _wait_status(
        started.run_id, {"awaiting_phase_handoff"}, timeout_s=30.0,
    )
    assert paused == "awaiting_phase_handoff"

    # Read the active handoff payload: handoff_id + available_actions
    # are decided by the runtime, not the client.
    snap = orcho_run_status(started.run_id)
    handoff = (snap.meta or {})["phase_handoff"]
    handoff_id = handoff["id"]
    assert handoff["phase"] == "validate_plan"
    assert "continue" in set(handoff["available_actions"])

    # 3. INSPECT — typed reviewer findings, no log scraping.
    findings_bundle = orcho_run_evidence(
        started.run_id, slice="findings", phases=["validate_plan"],
    )
    assert findings_bundle.findings is not None
    findings = findings_bundle.findings
    assert findings, "validate_plan handoff paused but produced no findings"

    f = findings[0]
    assert f.severity in {"P0", "P1", "P2", "P3"}, (
        f"unexpected severity: {f.severity!r}"
    )
    assert f.title
    assert f.body
    assert f.required_fix, (
        "DEMO-1B mock validate_plan findings should carry required_fix"
    )
    assert f.phase == "validate_plan"

    # 4. DECIDE — record a continue override.
    decision = await orcho_phase_handoff_decide(
        started.run_id,
        handoff_id=handoff_id,
        action="continue",
        note="DEMO-1B continues past the forced mock validate_plan handoff.",
    )
    assert decision.run_id == started.run_id
    assert decision.handoff_id == handoff_id
    assert decision.action == "continue"
    assert decision.decided_at  # ISO timestamp

    # 5. ACT — resume from checkpoint.
    resumed = await orcho_run_resume(started.run_id, profile="task")
    assert resumed.run_id == started.run_id

    # 6. OBSERVE — wait for terminal. The accept set is wide so a
    # non-`done` terminal produces a useful assertion message rather
    # than a timeout, but DEMO-1B is a proof loop: only `done` passes.
    final = await _wait_status(
        started.run_id,
        {"done", "failed", "halted", "interrupted"},
        timeout_s=90.0,
    )
    assert final == "done", (
        f"DEMO-1B requires terminal=done after approve+resume; got {final!r}"
    )

    # 7. INSPECT — final evidence, metrics, history through the public
    # MCP surface. No file paths, no meta.json reads, no events.jsonl.
    full = orcho_run_evidence(started.run_id, slice="all")
    assert full.plan is not None
    assert full.findings is not None
    assert full.errors is not None

    # Findings persist past the gate — the same plan_qa records the
    # decision approved are still inspectable after terminal.
    assert any(rec.phase == "validate_plan" for rec in full.findings)

    metrics_result = orcho_run_metrics(started.run_id)
    assert metrics_result.run_id == started.run_id
    metrics = metrics_result.metrics
    assert metrics, "metrics rollup should be populated after a done run"

    history = orcho_run_history(limit=10)
    assert any(r.run_id == started.run_id for r in history.runs), (
        "the just-finished run should appear in recent history"
    )
