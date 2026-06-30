"""Acceptance smoke for the ``auto-detect`` profile selector via ``--mock``.

End-to-end through the supervisor: spawn a real
``pipeline.project_orchestrator`` subprocess with ``profile='auto-detect'``
in mock mode, then assert the run reached ``done`` AND that
``orcho_run_status`` surfaces the typed ``auto_detect`` projection sourced
from core's persisted ``meta.auto_detect`` (NOT fabricated MCP-side).

Under ``--mock`` orcho-core's CLI installs a ``StaticWorkKindDetector`` that
deterministically recommends the default profile (``feature``) at mode
``fast`` with confidence 1.0 (cli.py auto-detect dispatch), so the resolved
decision is a clean ``recommended`` state. This is the cross-repo
mock-smoke required by AGENTS.md for the auto-detect wire surface.

Mock mode is fully hermetic — no real provider CLI calls fire. Marked
``mcp_integration`` so the default run skips it; opt-in with
``pytest -m mcp_integration``.
"""
from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = pytest.mark.mcp_integration


@pytest.fixture
def mock_project(tmp_path, monkeypatch):
    """Create a minimal orcho-trackable project + workspace.

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
async def test_orcho_run_auto_detect_status_projection(mock_project):
    """``profile='auto-detect'`` reaches ``done`` and surfaces a typed,
    core-sourced ``auto_detect`` projection through ``orcho_run_status``.

    Pins both the T1 spawn contract (the selector threads as
    ``--profile auto-detect``, not via ``ORCHO_PIPELINE``) and the T2
    status projection against a REAL subprocess decision: the
    ``requested_selector`` / ``selected_*`` values must come from core's
    persisted ``meta.auto_detect`` block, proving MCP does not pre-resolve
    or fabricate the decision.
    """
    from orcho_mcp.supervisor import RunsSupervisor
    from orcho_mcp.tools import orcho_run_status

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="trivial mock task — auto-detect smoke",
        project_dir=str(mock_project),
        profile="auto-detect",
        mock=True,
        max_rounds=1,
    )

    # Wait for the pipeline subprocess to exit.
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if handle.popen is not None and handle.popen.poll() is not None:
            break
        await asyncio.sleep(0.5)
    else:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.kill()
        pytest.fail("mock auto-detect pipeline didn't finish within 90s")

    # Let the reap task update supervisor state.
    for _ in range(50):
        if handle.status != "running":
            break
        await asyncio.sleep(0.1)

    assert handle.exit_code == 0, (
        f"mock auto-detect pipeline exited rc={handle.exit_code}; "
        f"runner.log tail: "
        f"{(handle.run_dir / 'runner.log').read_text()[-2000:] if (handle.run_dir / 'runner.log').is_file() else '(no log)'}"
    )
    assert handle.status == "done"

    snap = orcho_run_status(handle.run_id)
    assert snap.meta.get("status") == "done"

    # Core persisted meta.auto_detect because the run started through the
    # auto-detect selector channel. If it is absent the selector did not
    # route through argv (a T1 regression) or core stopped writing the
    # block — surface that precisely rather than masking it.
    raw_auto_detect = snap.meta.get("auto_detect")
    assert raw_auto_detect is not None, (
        "meta.auto_detect absent — auto-detect selector did not materialise "
        "the core decision block (check T1 argv routing / core write path); "
        f"meta keys: {sorted(snap.meta.keys())}"
    )

    ad = snap.auto_detect
    assert ad is not None, "RunStatus.auto_detect projection missing"
    # requested_selector is the core selector token (presence invariant).
    assert ad.requested_selector == "auto-detect"
    # selected_* come from core's StaticWorkKindDetector under mock — assert
    # the projection mirrors the persisted meta, proving no MCP fabrication.
    assert ad.selected_profile == "feature"
    assert ad.selected_mode == "fast"
    assert ad.detection_state == "recommended"
    assert ad.trusted is True
    # A clean recommendation needs no operator step.
    assert ad.next_action is None
    # The projection's selected_* must equal what core actually wrote.
    assert ad.selected_profile == raw_auto_detect.get("actual_profile")
    assert ad.selected_mode == raw_auto_detect.get("actual_mode")


@pytest.mark.asyncio
async def test_orcho_run_auto_detect_surfaces_topology_for_wire_signal(mock_project):
    """A cross-signal task surfaces the topology axis end-to-end (F2).

    Mock E2E for the topology / delivery-scope projection: spawn a real
    ``profile='auto-detect'`` subprocess in mock mode with a task that
    carries SDK-wire + MCP-schema signals. The mock detector applies the
    SAME deterministic topology heuristic the provider path does
    (``cli._build_mock_work_kind_detector`` → ``recommend_topology``), so
    core persists ``recommended_topology='cross_recommended'`` plus the
    union ``delivery_projects`` into ``meta.auto_detect``. The MCP
    ``orcho_run_status`` projection must echo those fields verbatim and
    expose the three typed topology choices — never widening delivery
    (``delivery_scope`` stays ``strict_mono``; the recommendation is
    advisory).

    Mandatory acceptance smoke (T5 Done Criteria): this MUST run against the
    current orcho-core (worktree or promoted) that carries the topology axis.
    It asserts ``recommended_topology`` / ``delivery_scope`` / ``projects`` /
    ``topology_next_actions`` UNCONDITIONALLY — a core that predates the axis
    makes it FAIL, not skip, so a green run can never hide a missing topology
    projection. (Backward-compat against an old core is covered separately by
    the synthetic-meta unit tests in
    ``tests/unit/services/test_auto_detect_projection.py``.)
    """
    from orcho_mcp.supervisor import RunsSupervisor
    from orcho_mcp.tools import orcho_run_status

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task=(
            "Change the core SDK wire format and regenerate the MCP schema "
            "snapshot so the orcho-mcp tool stays in sync."
        ),
        project_dir=str(mock_project),
        profile="auto-detect",
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
        pytest.fail("mock auto-detect pipeline didn't finish within 90s")

    for _ in range(50):
        if handle.status != "running":
            break
        await asyncio.sleep(0.1)

    assert handle.exit_code == 0, (
        f"mock auto-detect pipeline exited rc={handle.exit_code}; "
        f"runner.log tail: "
        f"{(handle.run_dir / 'runner.log').read_text()[-2000:] if (handle.run_dir / 'runner.log').is_file() else '(no log)'}"
    )
    assert handle.status == "done"

    snap = orcho_run_status(handle.run_id)
    raw_auto_detect = snap.meta.get("auto_detect")
    assert raw_auto_detect is not None, "meta.auto_detect absent"

    ad = snap.auto_detect
    assert ad is not None, "RunStatus.auto_detect projection missing"
    # The E2E auto-detect path is always exercised: semantic profile resolves
    # to the mock default and is mirrored from core's persisted block.
    assert ad.requested_selector == "auto-detect"
    assert ad.selected_profile == raw_auto_detect.get("actual_profile")

    # MANDATORY: core MUST have produced the topology axis for a cross-signal
    # task. Absence is a hard failure (not a skip), so a green run can never
    # mask a broken / stale core or a regressed topology projection.
    assert raw_auto_detect.get("recommended_topology") is not None, (
        "meta.auto_detect carries no recommended_topology for a cross-signal "
        "task: the spawned orcho-core predates the topology axis (T1/T2) or "
        "regressed. Run this acceptance smoke against the current core "
        "(worktree/promoted) that carries the topology axis. "
        f"auto_detect keys: {sorted(raw_auto_detect.keys())}"
    )

    # Core produced the topology axis → assert the MCP projection echoes it.
    assert ad.recommended_topology == "cross_recommended"
    assert "orcho-core" in ad.projects
    assert "orcho-mcp" in ad.projects
    assert ad.topology_reason
    # The recommendation never widens delivery on the non-interactive path.
    assert ad.delivery_scope == "strict_mono"
    # Three typed choices, each with a stable machine-readable selector (F1).
    assert ad.topology_next_actions, "topology_next_actions must be populated"
    selectors = [r.args.get("topology_choice") for r in ad.topology_next_actions]
    assert selectors == ["start_cross", "expanded_mono", "strict_mono"]


@pytest.fixture
def anyio_backend():
    return "asyncio"
