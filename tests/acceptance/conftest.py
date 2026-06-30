"""Shared fixtures for the L4 (mcp_integration) acceptance layer.

Hosts the supervisor isolation discipline and synthetic mock-project
fixtures used by the ``tests/acceptance/mock_pipeline/`` suite. Any
file under ``tests/acceptance/`` inherits these automatically through
pytest's standard conftest scoping; the fixtures intentionally do not
reach other layers.

Acceptance-only fixtures live here so their autouse behaviour does not
reach unit or protocol tests.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _supervisor_reset(monkeypatch):
    """Per-test isolation for the supervisor singleton.

    The supervisor is module-level (``get_supervisor`` → cached
    ``_singleton``) so its ``_runs`` dict accretes across tests in the
    same process. Each L4 test typically spawns 1-3 runs; without a
    reset the 5th test trips the default ``ORCHO_MCP_MAX_RUNS=4``
    cap not because the cap is wrong but because dead handles linger
    in memory while the reaper is still flipping their ``status``.

    Reset the singleton before *and* after each test so order doesn't
    matter, and bump the cap so a single test that spawns multiple
    runs isn't stalled by reaper races.
    """
    monkeypatch.setenv("ORCHO_MCP_MAX_RUNS", "20")
    import orcho_mcp.supervisor as _sup
    _sup._singleton = None
    yield
    _sup._singleton = None


@pytest.fixture
def mock_project(tmp_path: Path, monkeypatch) -> Path:
    """Synthetic workspace + project. ``ORCHO_WORKSPACE`` pinned for the test.

    The project dir is a real git repo: orcho-core's worktree resolver
    hard-fails when ``project_dir`` is not a git checkout with a HEAD
    (orcho-core ``3b516ec`` removed the silent degraded-off path). The
    shared ``init_git_repo`` helper added in ``f12b9ec`` wired the
    supervisor / pilot / observe parity fixtures through; this L4
    acceptance fixture was the remaining gap — every ``mcp_integration``
    subprocess spawned against a non-git project aborted with
    "project_dir is not a git repository", surfacing as
    interrupted/failed runs.
    """
    from tests.conftest import init_git_repo

    ws = tmp_path / "ws"
    project = ws / "demo_project"
    runs_dir = ws / "runspace" / "runs"
    project.mkdir(parents=True)
    runs_dir.mkdir(parents=True)
    (project / "README.md").write_text("# Demo project\n", encoding="utf-8")
    init_git_repo(project)  # commits README + .gitkeep → clean git HEAD
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    # Pin ORCHO_RUNSPACE too. ``core.infra.platform.runspace_dir`` resolves
    # ``$ORCHO_RUNSPACE`` *before* ``$ORCHO_WORKSPACE/runspace``; when the
    # suite runs under the Orcho orchestrator (which exports ORCHO_RUNSPACE
    # for the real workspace), an ORCHO_WORKSPACE-only override is shadowed
    # and the supervisor reads the real runs dir instead of this tmp one.
    # Pinning both keeps the fixture hermetic regardless of ambient env.
    monkeypatch.setenv("ORCHO_RUNSPACE", str(ws / "runspace"))
    return project


@pytest.fixture
def runs_dir(mock_project: Path) -> Path:
    """The ``<ws>/worktree/runs`` directory derived from ``mock_project``."""
    return mock_project.parent / "runspace" / "runs"
