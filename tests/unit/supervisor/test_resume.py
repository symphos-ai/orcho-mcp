"""Unit tests for ``RunsSupervisor.resume`` delegation to the SDK seam.

Resume now delegates the detached respawn (argv build, env,
``runner.log`` append, and the detached-session ``Popen``) to
``sdk.run_control.resume_run``. The mock/output_mode inheritance and the
``None`` → ``meta.profile`` → ``"feature"`` profile resolution live
inside that seam and are covered by the SDK's own tests.

These tests therefore assert the supervisor's own contract: the
MCP-side inspect-only / missing-task gates run before delegation, the
caller's ``profile`` is forwarded verbatim to ``resume_run`` along with
the resolved ``runs_dir``, and the returned ``LaunchResult`` is wrapped
into a ``RunHandle`` that mirrors the inherited ``mock`` / ``output_mode``
and is persisted to ``mcp_supervisor.json``.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from sdk.run_control.launch import LaunchedRun, LaunchResult

from orcho_mcp.errors import RunNotFoundError
from orcho_mcp.supervisor import RunsSupervisor


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _install_fake_resume(
    monkeypatch, captured, *, run_dir, project_dir,
    mock=False, output_mode="summary",
):
    """Patch the resume module's ``resume_run`` seam.

    Records the ``run_id`` / ``runs_dir`` / ``profile`` the supervisor
    forwards, then returns a ``LaunchResult`` around a real fast-exit
    child (so ``_reap`` can ``wait()``) whose ``run`` carries the
    inherited ``mock`` / ``output_mode`` the seam would have recovered
    from ``run_supervisor.json``.
    """
    real_popen = subprocess.Popen

    def fake_resume_run(run_id, *, runs_dir=None, profile=None):
        captured["run_id"] = run_id
        captured["runs_dir"] = runs_dir
        captured["profile"] = profile
        popen = real_popen([sys.executable, "-c", "import sys; sys.exit(0)"])
        run = LaunchedRun(
            run_id=run_id,
            pid=popen.pid,
            pgid=popen.pid,
            run_dir=Path(run_dir),
            project_dir=str(project_dir),
            command=[sys.executable, "-m", "pipeline.project_orchestrator"],
            started_at="2026-07-07T00:00:00.000Z",
            mock=mock,
            output_mode=output_mode,
        )
        return LaunchResult(run=run, popen=popen)

    monkeypatch.setattr(
        "orcho_mcp.supervisor.resume.resume_run", fake_resume_run
    )


def _reap_cleanup(handle):
    if handle.popen and handle.popen.poll() is None:
        handle.popen.terminate()
        handle.popen.wait(timeout=2)


def _write_resumable_run(workspace, run_id, project, meta):
    """Lay down a resumable run dir (meta + MCP supervisor state)."""
    run_dir = workspace / "runspace" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps(meta))
    (run_dir / "mcp_supervisor.json").write_text(json.dumps({
        "run_id": run_id,
        "pid": 99999999,
        "pgid": 99999999,
        "command": ["x"],
        "cwd": str(project),
        "project_dir": str(project),
        "started_at": "t",
        "status": "awaiting_phase_handoff",
        "mock": True,
        "output_mode": "summary",
    }))
    return run_dir


# ── argv builder + helper contracts (unchanged, SDK-adjacent) ───────────────

def test_resume_argv_emits_explicit_profile(tmp_path, monkeypatch):
    """The SDK argv builder emits ``--profile <name>`` + ``--resume``.

    Pins the argv contract ``resume_run`` relies on for the
    deliberate-profile-switch path (e.g. resume a paused ``feature`` run
    into ``planning``). Pure argv assertion, no subprocess.
    """
    from pipeline.argv import build_orch_argv

    argv = build_orch_argv(
        project="/p",
        resume="20260506_test_resume",
        run_id="20260506_test_resume",
        output_dir="/runs/20260506_test_resume",
        profile="planning",
    )
    assert "--profile" in argv
    assert argv[argv.index("--profile") + 1] == "planning"
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "20260506_test_resume"


def test_read_meta_profile_returns_recorded_profile(tmp_path):
    """``read_meta_profile`` extracts ``meta.profile`` for inherit
    resolution; returns None for missing / malformed / empty."""
    from orcho_mcp.supervisor.state import read_meta_profile

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # Missing meta.json → None
    assert read_meta_profile(run_dir) is None

    # Empty meta → None (no profile field)
    (run_dir / "meta.json").write_text(json.dumps({"task": "t"}))
    assert read_meta_profile(run_dir) is None

    # Whitespace-only profile → None
    (run_dir / "meta.json").write_text(json.dumps({"profile": "   "}))
    assert read_meta_profile(run_dir) is None

    # Valid profile string → returned verbatim
    (run_dir / "meta.json").write_text(json.dumps({"profile": "feature"}))
    assert read_meta_profile(run_dir) == "feature"

    # Non-string profile (corruption) → None (silent)
    (run_dir / "meta.json").write_text(json.dumps({"profile": 42}))
    assert read_meta_profile(run_dir) is None

    # Malformed JSON → None (silent)
    (run_dir / "meta.json").write_text("{ not json")
    assert read_meta_profile(run_dir) is None


# ── delegation: handle mirrors the seam result ──────────────────────────────

@pytest.mark.asyncio
async def test_resume_handle_mirrors_inherited_mock_and_output_mode(
    tmp_path, fake_workspace, monkeypatch,
):
    """The resumed handle echoes the ``mock`` / ``output_mode`` the seam
    recovered — a paused mock/debug run stays mock/debug on resume — and
    the values are persisted back to ``mcp_supervisor.json``."""
    project = tmp_path / "proj"
    project.mkdir()
    run_dir = _write_resumable_run(
        fake_workspace, "resume_debug", project, {"task": "continue"},
    )

    captured: dict[str, object] = {}
    _install_fake_resume(
        monkeypatch, captured,
        run_dir=run_dir, project_dir=project,
        mock=True, output_mode="debug",
    )

    sup = RunsSupervisor()
    handle = await sup.resume("resume_debug")
    try:
        # Seam received the run id + the resolved runs_dir.
        assert captured["run_id"] == "resume_debug"
        assert captured["runs_dir"] == str(
            fake_workspace / "runspace" / "runs"
        )
        # Handle mirrors the inherited launch options.
        assert handle.mock is True
        assert handle.output_mode == "debug"
        # And they are persisted to the MCP state delta.
        state = json.loads((run_dir / "mcp_supervisor.json").read_text())
        assert state["output_mode"] == "debug"
        assert state["mock"] is True
        assert state["status"] == "running"
    finally:
        _reap_cleanup(handle)


@pytest.mark.asyncio
async def test_resume_forwards_none_profile_to_seam(
    tmp_path, fake_workspace, monkeypatch,
):
    """``profile=None`` is forwarded verbatim; the seam owns the
    ``None`` → ``meta.profile`` → ``"feature"`` resolution."""
    project = tmp_path / "proj"
    project.mkdir()
    run_dir = _write_resumable_run(
        fake_workspace, "resume_inherit", project,
        {"task": "continue", "profile": "complex_feature"},
    )

    captured: dict[str, object] = {}
    _install_fake_resume(
        monkeypatch, captured, run_dir=run_dir, project_dir=project,
    )

    sup = RunsSupervisor()
    handle = await sup.resume("resume_inherit")  # profile=None
    try:
        assert captured["profile"] is None
    finally:
        _reap_cleanup(handle)


@pytest.mark.asyncio
async def test_resume_forwards_explicit_profile_to_seam(
    tmp_path, fake_workspace, monkeypatch,
):
    """An explicit ``profile=<name>`` (deliberate switch) is forwarded
    verbatim to the seam."""
    project = tmp_path / "proj"
    project.mkdir()
    run_dir = _write_resumable_run(
        fake_workspace, "resume_switch", project, {"task": "continue"},
    )

    captured: dict[str, object] = {}
    _install_fake_resume(
        monkeypatch, captured, run_dir=run_dir, project_dir=project,
    )

    sup = RunsSupervisor()
    handle = await sup.resume("resume_switch", profile="planning")
    try:
        assert captured["profile"] == "planning"
    finally:
        _reap_cleanup(handle)


# ── MCP-side gates run before delegation ────────────────────────────────────

@pytest.mark.asyncio
async def test_resume_missing_run_dir_raises_not_found(
    tmp_path, fake_workspace, monkeypatch,
):
    """No run dir → ``RunNotFoundError`` before the seam is touched."""
    called: list = []

    def fail_if_called(*a, **k):
        called.append(a)
        raise AssertionError("resume_run must not run for a missing run dir")

    monkeypatch.setattr(
        "orcho_mcp.supervisor.resume.resume_run", fail_if_called
    )

    sup = RunsSupervisor()
    with pytest.raises(RunNotFoundError, match="run not found"):
        await sup.resume("does_not_exist")
    assert called == []


@pytest.mark.asyncio
async def test_resume_without_mcp_supervisor_state_is_inspect_only(
    tmp_path, fake_workspace, monkeypatch,
):
    """A run dir without ``mcp_supervisor.json`` is inspect-only — resume
    is refused with the stable message before the seam is touched."""
    run_id = "foreign_run"
    run_dir = fake_workspace / "runspace" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({"task": "continue"}))

    called: list = []

    def fail_if_called(*a, **k):
        called.append(a)
        raise AssertionError("resume_run must not run for an inspect-only run")

    monkeypatch.setattr(
        "orcho_mcp.supervisor.resume.resume_run", fail_if_called
    )

    sup = RunsSupervisor()
    with pytest.raises(RunNotFoundError, match="no mcp_supervisor.json"):
        await sup.resume(run_id)
    assert called == []


@pytest.mark.asyncio
async def test_resume_missing_meta_task_raises_not_found(
    tmp_path, fake_workspace, monkeypatch,
):
    """An MCP-owned run whose ``meta.json`` never recorded a task → stable
    missing-task ``RunNotFoundError`` before the seam is touched."""
    project = tmp_path / "proj"
    project.mkdir()
    # Resumable MCP state present, but meta.json has no task.
    _write_resumable_run(
        fake_workspace, "resume_no_task", project, {"not_task": "x"},
    )

    called: list = []

    def fail_if_called(*a, **k):
        called.append(a)
        raise AssertionError("resume_run must not run when meta task missing")

    monkeypatch.setattr(
        "orcho_mcp.supervisor.resume.resume_run", fail_if_called
    )

    sup = RunsSupervisor()
    with pytest.raises(RunNotFoundError, match="missing 'task'"):
        await sup.resume("resume_no_task")
    assert called == []
