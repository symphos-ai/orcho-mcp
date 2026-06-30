"""Unit tests for ``resolve_project_dir`` + the spawn-level guarantee
that relative ``project_dir`` is resolved once at entry.

Regression: passing a relative ``project_dir`` like ``"proj"`` used
to set ``Popen(cwd="proj")`` AND ``--project proj`` simultaneously,
causing the orchestrator to re-resolve ``--project`` against the
already-changed subprocess cwd and double the segment (``proj/proj``).
Spawn must resolve once at entry and use the absolute path for both.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from orcho_mcp.errors import PipelineSpawnError
from orcho_mcp.supervisor import RunsSupervisor
from orcho_mcp.supervisor.paths import resolve_project_dir, resolve_task_file


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── resolve_project_dir unit cases ─────────────────────────────────────────

def test_resolve_project_dir_absolute_passthrough(tmp_path):
    resolved = resolve_project_dir(str(tmp_path))
    assert resolved == str(tmp_path.resolve())


def test_resolve_project_dir_relative_resolves_against_cwd(tmp_path, monkeypatch):
    sub = tmp_path / "proj"
    sub.mkdir()
    monkeypatch.chdir(tmp_path)
    resolved = resolve_project_dir("proj")
    assert resolved == str(sub.resolve())
    assert Path(resolved).is_absolute()


def test_resolve_project_dir_dot_resolves_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    resolved = resolve_project_dir(".")
    assert resolved == str(tmp_path.resolve())


def test_resolve_project_dir_missing_raises(tmp_path):
    with pytest.raises(PipelineSpawnError, match="does not exist"):
        resolve_project_dir(str(tmp_path / "nope"))


def test_resolve_project_dir_empty_raises():
    with pytest.raises(PipelineSpawnError, match="non-empty"):
        resolve_project_dir("")


def test_resolve_project_dir_whitespace_raises():
    with pytest.raises(PipelineSpawnError, match="non-empty"):
        resolve_project_dir("   ")


# ── resolve_task_file unit cases ───────────────────────────────────────────

def test_resolve_task_file_absolute_file_passthrough(tmp_path):
    task = tmp_path / "task.md"
    task.write_text("do it\n", encoding="utf-8")

    resolved = resolve_task_file(str(task), project_dir=str(tmp_path))

    assert resolved == str(task.resolve())


def test_resolve_task_file_rejects_missing_absolute_path(tmp_path):
    missing = tmp_path / "plans" / "missing.md"

    with pytest.raises(PipelineSpawnError) as exc:
        resolve_task_file(str(missing), project_dir=str(tmp_path))

    message = str(exc.value)
    assert "--task-file not found" in message
    assert str(missing) in message
    assert ".orcho/.task-files" in message


def test_resolve_task_file_short_name_uses_reserved_project_dir(tmp_path):
    task_dir = tmp_path / ".orcho" / ".task-files"
    task_dir.mkdir(parents=True)
    task = task_dir / "task.md"
    task.write_text("do it\n", encoding="utf-8")

    resolved = resolve_task_file("task.md", project_dir=str(tmp_path))

    assert resolved == str(task.resolve())


def test_resolve_task_file_missing_short_name_lists_reserved_dirs(tmp_path):
    with pytest.raises(PipelineSpawnError) as exc:
        resolve_task_file("missing.md", project_dir=str(tmp_path))

    message = str(exc.value)
    assert "--task-file short name not found: missing.md" in message
    assert str(tmp_path / ".orcho" / ".task-files") in message
    assert "direct relative/absolute path" in message


def test_resolve_task_file_relative_path_resolves_against_project(tmp_path):
    task_dir = tmp_path / "plans"
    task_dir.mkdir()
    task = task_dir / "task.md"
    task.write_text("do it\n", encoding="utf-8")

    resolved = resolve_task_file("plans/task.md", project_dir=str(tmp_path))

    assert resolved == str(task.resolve())


# ── end-to-end: spawn applies the resolution ────────────────────────────────

@pytest.mark.asyncio
async def test_spawn_resolves_relative_project_dir_into_absolute_paths(
    tmp_path, fake_workspace, monkeypatch,
):
    """Regression: passing a relative ``project_dir`` like ``"proj"`` used
    to set ``Popen(cwd="proj")`` AND ``--project proj`` simultaneously,
    causing the orchestrator to re-resolve ``--project`` against the
    already-changed subprocess cwd and double the segment (``proj/proj``).
    Spawn must resolve once at entry and use the absolute path for both.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(tmp_path)

    captured: dict[str, object] = {}
    real_popen = subprocess.Popen

    def capture_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["cwd"] = kwargs.get("cwd")
        return real_popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            **{k: v for k, v in kwargs.items() if k != "env"},
            env=kwargs.get("env"),
        )

    monkeypatch.setattr(subprocess, "Popen", capture_popen)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task="snapshot",
        project_dir="proj",
        mock=True,
    )
    try:
        abs_proj = str(proj.resolve())

        # cwd must be the absolute resolved path, not the raw "proj".
        assert captured["cwd"] == abs_proj

        # --project argv flag must carry the same absolute path.
        cmd = captured["cmd"]
        assert "--project" in cmd
        assert cmd[cmd.index("--project") + 1] == abs_proj

        # Handle and persisted state must record the absolute form too,
        # so resume can re-launch without re-applying cwd resolution.
        assert handle.project_dir == abs_proj
        state = json.loads(
            (handle.run_dir / "mcp_supervisor.json").read_text()
        )
        assert state["project_dir"] == abs_proj
        assert state["cwd"] == abs_proj
    finally:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.terminate()
            handle.popen.wait(timeout=2)


@pytest.mark.asyncio
async def test_spawn_rejects_missing_project_dir(tmp_path, fake_workspace):
    sup = RunsSupervisor()
    with pytest.raises(PipelineSpawnError, match="does not exist"):
        await sup.spawn(
            task="x",
            project_dir=str(tmp_path / "does_not_exist"),
            mock=True,
        )


@pytest.mark.asyncio
async def test_spawn_resolves_short_task_file_before_building_argv(
    tmp_path, fake_workspace, monkeypatch,
):
    proj = tmp_path / "proj"
    task_dir = proj / ".orcho" / ".task-files"
    task_dir.mkdir(parents=True)
    task_file = task_dir / "task.md"
    task_file.write_text("do it\n", encoding="utf-8")

    captured: dict[str, object] = {}
    real_popen = subprocess.Popen

    def capture_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return real_popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            **{k: v for k, v in kwargs.items() if k != "env"},
            env=kwargs.get("env"),
        )

    monkeypatch.setattr(subprocess, "Popen", capture_popen)

    sup = RunsSupervisor()
    handle = await sup.spawn(
        task_file="task.md",
        project_dir=str(proj),
        mock=True,
    )
    try:
        cmd = captured["cmd"]
        assert "--task-file" in cmd
        assert cmd[cmd.index("--task-file") + 1] == str(task_file.resolve())
    finally:
        if handle.popen and handle.popen.poll() is None:
            handle.popen.terminate()
            handle.popen.wait(timeout=2)


@pytest.mark.asyncio
async def test_spawn_rejects_missing_task_file_before_creating_run_dir(
    tmp_path, fake_workspace,
):
    sup = RunsSupervisor()
    runs_dir = fake_workspace / "runspace" / "runs"
    runs_before = set(runs_dir.iterdir())

    with pytest.raises(PipelineSpawnError, match="short name not found"):
        await sup.spawn(
            task_file="missing.md",
            project_dir=str(tmp_path),
            mock=True,
        )

    assert set(runs_dir.iterdir()) == runs_before
