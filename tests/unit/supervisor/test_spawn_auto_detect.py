"""Unit tests for ``RunsSupervisor.spawn`` auto-detect selector threading.

``profile="auto-detect"`` is a run-start *selector token* — orcho-core's CLI
resolves it into a concrete profile + mode before any profile-registry lookup
(see ``pipeline.project.cli`` auto-detect dispatch, keyed on the argv
``--profile`` value).

Spawn now delegates argv/env construction to
``sdk.run_control.launch.launch_run``. The rule that ``auto-detect`` must
reach core only through argv — never as an ``ORCHO_PIPELINE`` env override,
even when the MCP server inherited that env var — now lives inside that SDK
seam and is covered by its own unit tests plus the L4 mock smoke. The
supervisor's contract at this layer is narrower: it forwards the caller's
selector verbatim on ``LaunchSpec.profile`` and does not let an ambient
``ORCHO_PIPELINE`` value rewrite the spec's profile selection.
"""
from __future__ import annotations

import subprocess
import sys

import pytest
from sdk.run_control.launch import LaunchedRun, LaunchResult

from orcho_mcp.supervisor import RunsSupervisor


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _install_fake_launch(monkeypatch, fake_workspace, captured):
    """Patch the spawn module's ``launch_run`` seam, recording the spec."""
    runs_dir = fake_workspace / "runspace" / "runs"
    real_popen = subprocess.Popen

    def fake_launch_run(spec, *, run_id=None):
        captured["spec"] = spec
        captured["run_id"] = run_id
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        popen = real_popen([sys.executable, "-c", "import sys; sys.exit(0)"])
        run = LaunchedRun(
            run_id=run_id,
            pid=popen.pid,
            pgid=popen.pid,
            run_dir=run_dir,
            project_dir=spec.project_dir,
            command=[sys.executable, "-m", "pipeline.project_orchestrator"],
            started_at="2026-07-07T00:00:00.000Z",
            mock=spec.mock,
            output_mode=spec.output_mode,
        )
        return LaunchResult(run=run, popen=popen)

    monkeypatch.setattr(
        "orcho_mcp.supervisor.spawn.launch_run", fake_launch_run
    )


def _reap_cleanup(handle):
    if handle.popen and handle.popen.poll() is None:
        handle.popen.terminate()
        handle.popen.wait(timeout=2)


@pytest.mark.asyncio
async def test_spawn_threads_auto_detect_selector_onto_launch_spec(
    tmp_path, fake_workspace, monkeypatch,
):
    """``profile="auto-detect"`` lands verbatim on ``LaunchSpec.profile``."""
    monkeypatch.delenv("ORCHO_PIPELINE", raising=False)

    captured: dict[str, object] = {}
    _install_fake_launch(monkeypatch, fake_workspace, captured)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="auto-detect selector smoke",
        project_dir=str(tmp_path),
        profile="auto-detect",
        mock=True,
    )
    try:
        assert captured["spec"].profile == "auto-detect"
    finally:
        _reap_cleanup(handle)


@pytest.mark.asyncio
async def test_spawn_selector_spec_is_independent_of_inherited_auto_detect_env(
    tmp_path, fake_workspace, monkeypatch,
):
    """An inherited ``ORCHO_PIPELINE=auto-detect`` must not change the spec.

    The supervisor forwards the caller's ``profile="auto-detect"`` selector
    onto the spec regardless of ambient env; the SDK seam owns stripping any
    inherited override off the child process env.
    """
    monkeypatch.setenv("ORCHO_PIPELINE", "auto-detect")

    captured: dict[str, object] = {}
    _install_fake_launch(monkeypatch, fake_workspace, captured)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="auto-detect inherited env smoke",
        project_dir=str(tmp_path),
        profile="auto-detect",
        mock=True,
    )
    try:
        assert captured["spec"].profile == "auto-detect"
    finally:
        _reap_cleanup(handle)


@pytest.mark.asyncio
async def test_spawn_selector_spec_is_independent_of_inherited_concrete_env(
    tmp_path, fake_workspace, monkeypatch,
):
    """A concrete inherited ``ORCHO_PIPELINE`` cannot shadow the selector spec."""
    monkeypatch.setenv("ORCHO_PIPELINE", "complex_feature")

    captured: dict[str, object] = {}
    _install_fake_launch(monkeypatch, fake_workspace, captured)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="auto-detect with concrete inherited override",
        project_dir=str(tmp_path),
        profile="auto-detect",
        mock=True,
    )
    try:
        assert captured["spec"].profile == "auto-detect"
    finally:
        _reap_cleanup(handle)


@pytest.mark.asyncio
async def test_spawn_threads_concrete_profile_onto_launch_spec(
    tmp_path, fake_workspace, monkeypatch,
):
    """Control: a concrete non-feature profile threads verbatim onto the spec.

    Guards against the auto-detect handling accidentally rewriting the profile
    selection for genuine registered profiles.
    """
    monkeypatch.delenv("ORCHO_PIPELINE", raising=False)

    captured: dict[str, object] = {}
    _install_fake_launch(monkeypatch, fake_workspace, captured)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="concrete profile control",
        project_dir=str(tmp_path),
        profile="complex_feature",
        mock=True,
    )
    try:
        assert captured["spec"].profile == "complex_feature"
    finally:
        _reap_cleanup(handle)
