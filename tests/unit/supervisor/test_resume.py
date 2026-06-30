"""Unit tests for ``RunsSupervisor.resume`` and related helpers.

Covers the explicit-profile resume argv contract (build_orch_argv
emits ``--profile <name>`` + ``--resume <run_id>``), the
``read_meta_profile`` helper that drives inherit-from-meta
resolution, and the end-to-end resume path that preserves
``output_mode`` from the supervisor state file.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from orcho_mcp.supervisor import RunsSupervisor


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_resume_argv_emits_explicit_profile(tmp_path, monkeypatch):
    """When the caller passes an explicit ``profile=<name>`` to
    ``orcho_run_resume``, the supervisor must propagate it as
    ``--profile <name>`` in argv. This is the deliberate-profile-switch
    path: e.g. resume a paused ``feature`` run into the ``planning``
    profile to refine the plan only.

    Inherit-from-meta (``profile=None``) is tested separately in
    ``test_resume_inherits_profile_from_meta`` — that path is driven
    by ``Supervisor.resume`` itself, not by ``build_orch_argv`` (which
    is a pure argv builder).
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


@pytest.mark.asyncio
async def test_resume_preserves_output_mode_from_state(
    tmp_path, fake_workspace, monkeypatch,
):
    run_id = "resume_debug"
    run_dir = fake_workspace / "runspace" / "runs" / run_id
    run_dir.mkdir(parents=True)
    project = tmp_path / "proj"
    project.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"task": "continue"}))
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
        "output_mode": "debug",
    }))

    captured_cmd: list[str] = []
    real_popen = subprocess.Popen

    def capture_popen(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return real_popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            **{k: v for k, v in kwargs.items() if k != "env"},
            env=kwargs.get("env"),
        )

    monkeypatch.setattr(subprocess, "Popen", capture_popen)

    sup = RunsSupervisor()
    handle = await sup.resume(run_id)
    try:
        assert "--output" in captured_cmd
        assert captured_cmd[captured_cmd.index("--output") + 1] == "debug"
        assert "--workspace" in captured_cmd
        assert (
            captured_cmd[captured_cmd.index("--workspace") + 1]
            == str(fake_workspace)
        )
        state = json.loads((run_dir / "mcp_supervisor.json").read_text())
        assert state["output_mode"] == "debug"
    finally:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.terminate()
            handle.popen.wait(timeout=2)


def _write_resumable_run(workspace, run_id, project, meta):
    """Lay down a resumable run dir (meta + supervisor state)."""
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


@pytest.mark.asyncio
async def test_resume_inherits_profile_from_meta(
    tmp_path, fake_workspace, monkeypatch,
):
    """``profile=None`` resume inherits ``meta.profile`` verbatim — the
    review/final prompt envelope must match the original run's profile."""
    project = tmp_path / "proj"
    project.mkdir()
    _write_resumable_run(
        fake_workspace, "resume_inherit", project,
        {"task": "continue", "profile": "complex_feature"},
    )

    captured_cmd: list[str] = []
    real_popen = subprocess.Popen

    def capture_popen(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return real_popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            **{k: v for k, v in kwargs.items() if k != "env"},
            env=kwargs.get("env"),
        )

    monkeypatch.setattr(subprocess, "Popen", capture_popen)

    sup = RunsSupervisor()
    handle = await sup.resume("resume_inherit")  # profile=None → inherit
    try:
        assert "--profile" in captured_cmd
        assert (
            captured_cmd[captured_cmd.index("--profile") + 1]
            == "complex_feature"
        )
    finally:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.terminate()
            handle.popen.wait(timeout=2)


@pytest.mark.asyncio
async def test_resume_falls_back_to_feature_when_meta_has_no_profile(
    tmp_path, fake_workspace, monkeypatch,
):
    """Legacy runs whose ``meta.json`` predates profile capture fall back
    to the semantic default ``feature`` (not the retired ``advanced``)."""
    project = tmp_path / "proj"
    project.mkdir()
    _write_resumable_run(
        fake_workspace, "resume_fallback", project,
        {"task": "continue"},  # no profile recorded
    )

    captured_cmd: list[str] = []
    real_popen = subprocess.Popen

    def capture_popen(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return real_popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            **{k: v for k, v in kwargs.items() if k != "env"},
            env=kwargs.get("env"),
        )

    monkeypatch.setattr(subprocess, "Popen", capture_popen)

    sup = RunsSupervisor()
    handle = await sup.resume("resume_fallback")  # profile=None, no meta
    try:
        assert "--profile" in captured_cmd
        assert captured_cmd[captured_cmd.index("--profile") + 1] == "feature"
    finally:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.terminate()
            handle.popen.wait(timeout=2)
