"""Unit tests for ``RunsSupervisor.spawn`` argv/env threading.

Covers profile propagation via ``ORCHO_PIPELINE`` env + ``--profile``
argv, attachment expansion, output_mode threading, invalid output_mode
fast-fail, profile-driven session shape (full ``mock=True`` run),
``ORCHO_PIPELINE`` omission when no profile, and ``--from-run-plan``
forwarding.

Most tests capture the Popen call rather than running the real
``pipeline.project_orchestrator``; one drives an actual mock pipeline
to validate the small_task profile's session shape end-to-end.
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


# ── profile env propagation ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_spawn_threads_profile_via_orcho_pipeline_env(
    tmp_path, fake_workspace, monkeypatch,
):
    """Phase 5b post-script: when ``profile`` is supplied to ``spawn``,
    it must propagate to the subprocess via ``ORCHO_PIPELINE`` env var.
    Today this is plumbing-only (orcho-core's _PipelineRun ignores
    ORCHO_PIPELINE); Phase 5c wires the dispatcher and the parameter
    becomes meaningful without further MCP changes.

    Regression: pre-fix, the ``profile`` kwarg was silently dropped by
    ``supervisor.spawn`` — surfaced by Phase 5b MCP smoke testing.
    """
    captured_env: dict[str, str] = {}
    captured_argv: list[str] = []
    real_popen = subprocess.Popen

    def capture_popen(cmd, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        captured_argv.extend(cmd)
        # Substitute a no-op fast-exit child so the test runs in ms.
        return real_popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            **{k: v for k, v in kwargs.items() if k != "env"},
            env=kwargs.get("env"),
        )

    monkeypatch.setattr(subprocess, "Popen", capture_popen)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="snapshot",
        project_dir=str(tmp_path),
        # non-default semantic profile so both argv flag AND env are set
        profile="small_task",
        mock=True,
    )
    try:
        # Profile threads through argv ``--profile`` flag.
        # ``ORCHO_PIPELINE`` env var is also set as an explicit override
        # (handy for sub-pipelines that bypass argv parsing).
        assert "--profile" in captured_argv
        assert captured_argv[captured_argv.index("--profile") + 1] == "small_task"
        assert captured_env.get("ORCHO_PIPELINE") == "small_task"
        assert captured_env.get("ORCHO_RUN_ID") == handle.run_id
    finally:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.terminate()
            handle.popen.wait(timeout=2)


@pytest.mark.asyncio
async def test_spawn_threads_attachments_into_argv(
    tmp_path, fake_workspace, monkeypatch,
):
    """Attachment parameters propagate through ``build_orch_argv``.

    ``orcho_run_start.attach`` / ``attach_text`` / ``attach_image`` /
    ``attach_binary`` each emit one ``--attach <path>`` argv pair;
    orcho-core's argparse accepts repeating flags.
    """
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
    handle = await sup.spawn(
        task="snapshot",
        project_dir=str(tmp_path),
        mock=True,
        attach=["spec.md", "logs/error.log"],
        attach_image=["mockup.png"],
    )
    try:
        # --attach for each auto-detect path, --attach-image for typed.
        attach_count = captured_cmd.count("--attach")
        assert attach_count == 2, f"expected 2 --attach, got {attach_count}"
        attach_image_count = captured_cmd.count("--attach-image")
        assert attach_image_count == 1
        assert "spec.md" in captured_cmd
        assert "logs/error.log" in captured_cmd
        assert "mockup.png" in captured_cmd
    finally:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.terminate()
            handle.popen.wait(timeout=2)


@pytest.mark.asyncio
async def test_spawn_threads_output_mode_into_argv(
    tmp_path, fake_workspace, monkeypatch,
):
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
    handle = await sup.spawn(
        task="snapshot",
        project_dir=str(tmp_path),
        mock=True,
        output_mode="debug",
    )
    try:
        assert "--output" in captured_cmd
        assert captured_cmd[captured_cmd.index("--output") + 1] == "debug"
    finally:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.terminate()
            handle.popen.wait(timeout=2)


@pytest.mark.asyncio
async def test_spawn_threads_session_mode_into_argv(
    tmp_path, fake_workspace, monkeypatch,
):
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
    handle = await sup.spawn(
        task="snapshot",
        project_dir=str(tmp_path),
        mock=True,
        session_mode="stateless",
    )
    try:
        assert "--session-mode" in captured_cmd
        assert captured_cmd[captured_cmd.index("--session-mode") + 1] == (
            "stateless"
        )
    finally:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.terminate()
            handle.popen.wait(timeout=2)


@pytest.mark.asyncio
async def test_spawn_rejects_invalid_output_mode(
    tmp_path, fake_workspace, monkeypatch,
):
    """Shared validator must fail fast — before any subprocess is launched."""
    popen_calls: list = []

    def fail_if_called(*args, **kwargs):
        popen_calls.append(args)
        raise AssertionError("Popen should not run with invalid output_mode")

    monkeypatch.setattr(subprocess, "Popen", fail_if_called)

    sup = RunsSupervisor()
    with pytest.raises(ValueError, match="invalid output mode"):
        await sup.spawn(
            task="snapshot",
            project_dir=str(tmp_path),
            mock=True,
            output_mode="loud",
        )
    assert popen_calls == []


@pytest.mark.asyncio
async def test_profile_param_drives_session_shape(
    tmp_path, fake_workspace,
):
    """``profile="small_task"`` drives the expected scoped session shape.

    When ``profile="small_task"`` is supplied, ORCHO_PIPELINE env
    propagates to the spawned subprocess and orcho-core dispatches via
    the small_task profile. Result: session contains the small_task
    scope phases: plan, validate_plan, and implement, with no review/fix
    rounds. ``small_task`` declares ``hypothesis.attempts=0``, so the
    hypothesis phase is intentionally absent on this profile.
    """
    from tests.conftest import init_git_repo
    project = tmp_path / "proj"
    init_git_repo(project)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="small_task scope smoke",
        project_dir=str(project),
        profile="small_task",
        mock=True,
        max_rounds=1,
    )
    if handle.popen:
        handle.popen.wait(timeout=10)

    meta_path = handle.run_dir / "meta.json"
    assert meta_path.exists(), "meta.json not produced"
    meta = json.loads(meta_path.read_text())
    phases = meta.get("phases", {})
    assert "plan" in phases, (
        f"profile=small_task must produce plan phase; got {sorted(phases)}"
    )
    assert "validate_plan" in phases, (
        f"profile=small_task must validate the plan; got {sorted(phases)}"
    )
    assert "implement" in phases, (
        f"profile=small_task must produce implement phase; got {sorted(phases)}"
    )
    assert phases.get("rounds") == [], (
        f"profile=small_task must skip review/fix rounds; "
        f"got {phases.get('rounds')}"
    )


@pytest.mark.asyncio
async def test_spawn_omits_orcho_pipeline_when_no_profile(
    tmp_path, fake_workspace, monkeypatch,
):
    """Without ``profile``, ``ORCHO_PIPELINE`` must not appear in the
    subprocess env (avoid stale values from parent process leaking)."""
    monkeypatch.delenv("ORCHO_PIPELINE", raising=False)

    captured_env: dict[str, str] = {}
    real_popen = subprocess.Popen

    def capture_popen(cmd, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        return real_popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            **{k: v for k, v in kwargs.items() if k != "env"},
            env=kwargs.get("env"),
        )

    monkeypatch.setattr(subprocess, "Popen", capture_popen)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="snapshot",
        project_dir=str(tmp_path),
        mock=True,
    )
    try:
        assert "ORCHO_PIPELINE" not in captured_env
    finally:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.terminate()
            handle.popen.wait(timeout=2)


@pytest.mark.asyncio
async def test_spawn_omits_orcho_pipeline_for_default_feature_profile(
    tmp_path, fake_workspace, monkeypatch,
):
    """The semantic default profile (``feature``) must NOT set
    ``ORCHO_PIPELINE``.

    The behavioural sentinel in ``supervisor/spawn.py`` only exports
    ``ORCHO_PIPELINE`` for a non-default profile (``profile != "feature"``),
    so an explicit ``profile="feature"`` is indistinguishable from the
    default and leaves the env override unset (``--profile feature`` still
    threads through argv)."""
    monkeypatch.delenv("ORCHO_PIPELINE", raising=False)

    captured_env: dict[str, str] = {}
    captured_argv: list[str] = []
    real_popen = subprocess.Popen

    def capture_popen(cmd, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        captured_argv.extend(cmd)
        return real_popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            **{k: v for k, v in kwargs.items() if k != "env"},
            env=kwargs.get("env"),
        )

    monkeypatch.setattr(subprocess, "Popen", capture_popen)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="snapshot",
        project_dir=str(tmp_path),
        profile="feature",
        mock=True,
    )
    try:
        assert "ORCHO_PIPELINE" not in captured_env
        # argv still carries the explicit --profile selection.
        assert "--profile" in captured_argv
        assert captured_argv[captured_argv.index("--profile") + 1] == "feature"
    finally:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.terminate()
            handle.popen.wait(timeout=2)


# ── --from-run-plan surface ─────────────────────────────────────────────────

def test_spawn_threads_from_run_plan_via_argv_builder():
    """``Supervisor.spawn(from_run_plan=...)`` must forward the spec
    to ``build_orch_argv`` so the child orchestrator sees a single
    ``--from-run-plan <spec>`` pair in its argv. Mirrors the
    ``--profile`` resume coverage above — pure argv assertion,
    no subprocess.

    The L1 test pins the argv contract; the L4 mock-integration smoke
    (gated under ``mcp_integration``) drives the actual subprocess.
    """
    from pipeline.argv import build_orch_argv

    argv = build_orch_argv(
        project="/p",
        task="follow-up",
        run_id="20260524_test_child",
        output_dir="/runs/20260524_test_child",
        profile="feature",
        from_run_plan="20260523_test_parent",
    )
    assert "--from-run-plan" in argv
    assert argv[argv.index("--from-run-plan") + 1] == (
        "20260523_test_parent"
    )
    # Without --from-run-plan supplied, the flag is absent.
    plain = build_orch_argv(
        project="/p", task="t",
        run_id="x", output_dir="/runs/x",
        profile="feature",
    )
    assert "--from-run-plan" not in plain


@pytest.mark.asyncio
async def test_spawn_forwards_from_run_plan_to_subprocess_argv(
    tmp_path, fake_workspace, monkeypatch,
):
    """``Supervisor.spawn(from_run_plan=...)`` must end-to-end land
    the flag in the spawned subprocess' argv. Captures the Popen
    command rather than actually running orcho-core (the child
    immediately exits via ``sys.exit(0)``), so the test verifies
    the wiring not the downstream effect (covered by orcho-core's
    own integration tests + the L4 mock smoke)."""
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
    handle = await sup.spawn(
        task="follow-up task",
        project_dir=str(tmp_path),
        mock=True,
        from_run_plan="20260523_test_parent",
    )
    try:
        assert "--from-run-plan" in captured_cmd
        idx = captured_cmd.index("--from-run-plan")
        assert captured_cmd[idx + 1] == "20260523_test_parent"
    finally:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.terminate()
            handle.popen.wait(timeout=2)
