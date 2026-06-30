"""Unit tests for ``RunsSupervisor.spawn`` auto-detect selector threading.

``profile="auto-detect"`` is a run-start *selector token* — orcho-core's CLI
resolves it into a concrete profile + mode before any profile-registry lookup
(see ``pipeline.project.cli`` auto-detect dispatch, keyed on the argv
``--profile`` value). The supervisor must therefore:

  * thread the selector through argv as ``--profile auto-detect`` (already
    handled by ``build_orch_argv``'s verbatim passthrough), and
  * NOT export ``ORCHO_PIPELINE=auto-detect`` — that env var is a concrete
    profile override that feeds straight into the registry resolver, so setting
    it to the selector token would pre-resolve / break the run.

A control case pins that a genuine non-feature profile still sets
``ORCHO_PIPELINE``.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from orcho_mcp.supervisor import RunsSupervisor


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _capture_popen(captured_env, captured_argv):
    """Build a Popen substitute that records env+argv then fast-exits."""
    real_popen = subprocess.Popen

    def capture_popen(cmd, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        captured_argv.extend(cmd)
        return real_popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            **{k: v for k, v in kwargs.items() if k != "env"},
            env=kwargs.get("env"),
        )

    return capture_popen


@pytest.mark.asyncio
async def test_spawn_threads_auto_detect_selector_via_argv_not_env(
    tmp_path, fake_workspace, monkeypatch,
):
    """``profile="auto-detect"`` lands in argv as a ``--profile auto-detect``
    pair but MUST NOT set ``ORCHO_PIPELINE`` to the selector token."""
    monkeypatch.delenv("ORCHO_PIPELINE", raising=False)

    captured_env: dict[str, str] = {}
    captured_argv: list[str] = []
    monkeypatch.setattr(
        subprocess, "Popen", _capture_popen(captured_env, captured_argv)
    )

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="auto-detect selector smoke",
        project_dir=str(tmp_path),
        profile="auto-detect",
        mock=True,
    )
    try:
        # Selector threads through argv as a contiguous --profile pair.
        assert "--profile" in captured_argv
        assert captured_argv[captured_argv.index("--profile") + 1] == (
            "auto-detect"
        )
        # The selector token must never become an ORCHO_PIPELINE override.
        assert captured_env.get("ORCHO_PIPELINE") != "auto-detect"
        assert "ORCHO_PIPELINE" not in captured_env
    finally:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.terminate()
            handle.popen.wait(timeout=2)


@pytest.mark.asyncio
async def test_spawn_strips_inherited_auto_detect_orcho_pipeline(
    tmp_path, fake_workspace, monkeypatch,
):
    """An INHERITED ``ORCHO_PIPELINE=auto-detect`` must not leak to the child.

    ``execute`` copies ``os.environ``; if the MCP server itself was launched
    with ``ORCHO_PIPELINE=auto-detect`` the subprocess would otherwise receive
    that concrete-profile override alongside argv ``--profile auto-detect`` and
    pre-resolve the run. The selector must reach core only through argv.
    """
    monkeypatch.setenv("ORCHO_PIPELINE", "auto-detect")

    captured_env: dict[str, str] = {}
    captured_argv: list[str] = []
    monkeypatch.setattr(
        subprocess, "Popen", _capture_popen(captured_env, captured_argv)
    )

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="auto-detect inherited env smoke",
        project_dir=str(tmp_path),
        profile="auto-detect",
        mock=True,
    )
    try:
        assert "--profile" in captured_argv
        assert captured_argv[captured_argv.index("--profile") + 1] == (
            "auto-detect"
        )
        # The inherited selector token is stripped from the child env.
        assert "ORCHO_PIPELINE" not in captured_env
    finally:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.terminate()
            handle.popen.wait(timeout=2)


@pytest.mark.asyncio
async def test_spawn_strips_inherited_concrete_orcho_pipeline(
    tmp_path, fake_workspace, monkeypatch,
):
    """A concrete inherited ``ORCHO_PIPELINE`` override cannot shadow auto-detect."""
    monkeypatch.setenv("ORCHO_PIPELINE", "complex_feature")

    captured_env: dict[str, str] = {}
    captured_argv: list[str] = []
    monkeypatch.setattr(
        subprocess, "Popen", _capture_popen(captured_env, captured_argv)
    )

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="auto-detect with concrete inherited override",
        project_dir=str(tmp_path),
        profile="auto-detect",
        mock=True,
    )
    try:
        assert captured_argv[captured_argv.index("--profile") + 1] == (
            "auto-detect"
        )
        # Auto-detect owns concrete profile selection in core. Any inherited
        # ORCHO_PIPELINE override could make the child run a different profile
        # than the one recorded in meta.auto_detect, so strip it.
        assert "ORCHO_PIPELINE" not in captured_env
    finally:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.terminate()
            handle.popen.wait(timeout=2)


@pytest.mark.asyncio
async def test_spawn_still_sets_orcho_pipeline_for_concrete_profile(
    tmp_path, fake_workspace, monkeypatch,
):
    """Control: a concrete non-feature profile still exports ``ORCHO_PIPELINE``.

    Guards against the auto-detect exclusion accidentally suppressing the
    env override for genuine registered profiles.
    """
    monkeypatch.delenv("ORCHO_PIPELINE", raising=False)

    captured_env: dict[str, str] = {}
    captured_argv: list[str] = []
    monkeypatch.setattr(
        subprocess, "Popen", _capture_popen(captured_env, captured_argv)
    )

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="concrete profile control",
        project_dir=str(tmp_path),
        profile="complex_feature",
        mock=True,
    )
    try:
        assert "--profile" in captured_argv
        assert captured_argv[captured_argv.index("--profile") + 1] == (
            "complex_feature"
        )
        assert captured_env.get("ORCHO_PIPELINE") == "complex_feature"
    finally:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.terminate()
            handle.popen.wait(timeout=2)
