"""Unit tests for ``orcho_run_start`` / ``orcho_run_resume`` /
``orcho_run_cancel`` argument threading and the run-control command /
error mapping taxonomy.

The supervisor is monkeypatched via ``orcho_mcp.supervisor.get_supervisor``
so the lazy import inside ``orcho_mcp.run_control.lifecycle`` picks up
the fake at call time without per-handler patching.

Command/error taxonomy (Work #3) the lifecycle layer must honour:

- successful command result → wire model unchanged (RunStartedResult /
  CancelResult);
- pending operator status → CancelResult status text unchanged;
- validation error → InvalidPlanError (incl. the invalid cancel-mode
  ``ValueError`` that leaks out of ``supervisor.cancel``);
- run not found → RunNotFoundError (already typed in supervisor →
  passes through);
- environment / import issue → WorkspaceNotResolvedError (typed in
  supervisor → passes through);
- supervisor / subprocess issue → PipelineSpawnError (typed in
  supervisor → passes through; any non-OrchoMCPError leak is
  translated here).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orcho_mcp.errors import (
    InvalidPlanError,
    PipelineSpawnError,
    RunNotFoundError,
    WorkspaceNotResolvedError,
)
from orcho_mcp.run_control.lifecycle import cancel_run, resume_run, start_run
from orcho_mcp.supervisor import RunHandle
from orcho_mcp.tools import orcho_run_resume, orcho_run_start


class _FakeSupervisor:
    """Configurable fake. Each operation either returns a canned result
    or raises a configured exception, so a single class drives every
    taxonomy branch."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        spawn_exc: BaseException | None = None,
        resume_exc: BaseException | None = None,
        cancel_exc: BaseException | None = None,
        cancel_status: str = "signal_sent(graceful)",
    ) -> None:
        self.tmp_path = tmp_path
        self.spawn_kwargs: dict | None = None
        self.resume_kwargs: dict | None = None
        self.cancel_kwargs: dict | None = None
        self._spawn_exc = spawn_exc
        self._resume_exc = resume_exc
        self._cancel_exc = cancel_exc
        self._cancel_status = cancel_status

    async def spawn(self, **kwargs):
        self.spawn_kwargs = kwargs
        if self._spawn_exc is not None:
            raise self._spawn_exc
        return RunHandle(
            run_id="fake_run",
            pid=123,
            pgid=123,
            run_dir=self.tmp_path,
            project_dir=kwargs.get("project_dir", "/p"),
            command=["python", "-m", "pipeline.project_orchestrator"],
            started_at="2026-05-07T12:00:00.000Z",
        )

    async def resume(self, run_id: str, *, profile: str = "feature"):
        self.resume_kwargs = {"run_id": run_id, "profile": profile}
        if self._resume_exc is not None:
            raise self._resume_exc
        return RunHandle(
            run_id=run_id,
            pid=124,
            pgid=124,
            run_dir=self.tmp_path,
            project_dir="/p",
            command=["python", "-m", "pipeline.project_orchestrator"],
            started_at="2026-05-07T12:00:00.000Z",
        )

    async def cancel(self, run_id: str, *, mode: str = "graceful"):
        self.cancel_kwargs = {"run_id": run_id, "mode": mode}
        # Mirror the real supervisor.cancel contract: a bad mode raises a
        # bare ValueError; the lifecycle boundary must translate it.
        if mode not in ("graceful", "hard"):
            raise ValueError(
                f"cancel mode must be 'graceful' or 'hard', got {mode!r}"
            )
        if self._cancel_exc is not None:
            raise self._cancel_exc
        return {"run_id": run_id, "status": self._cancel_status}


def _patch_supervisor(monkeypatch: pytest.MonkeyPatch, fake: _FakeSupervisor) -> None:
    monkeypatch.setattr("orcho_mcp.supervisor.get_supervisor", lambda: fake)


# ── success / argument threading ──────────────────────────────────────


@pytest.mark.asyncio
async def test_orcho_run_threads_profile_without_mode_arg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    await orcho_run_start(
        task="x", project_dir="/p", profile="small_task", mock=True,
    )

    assert fake.spawn_kwargs is not None
    assert fake.spawn_kwargs["profile"] == "small_task"
    assert "mode" not in fake.spawn_kwargs


@pytest.mark.asyncio
async def test_start_run_next_actions_ready_watch_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a successful spawn, ``next_actions`` carries exactly one
    ready ``orcho_run_watch`` call pre-filled with the new ``run_id`` so the
    client can enter the watch loop immediately."""
    fake = _FakeSupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    result = await start_run(task="x", project_dir="/p", mock=True)

    assert len(result.next_actions) == 1
    na = result.next_actions[0]
    assert na.kind == "ready_call"
    assert na.tool == "orcho_run_watch"
    assert na.args == {"run_id": "fake_run"}
    assert na.requires_operator_input is False


@pytest.mark.asyncio
async def test_orcho_run_threads_output_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    await orcho_run_start(
        task="x", project_dir="/p", mock=True, output_mode="debug",
    )

    assert fake.spawn_kwargs is not None
    assert fake.spawn_kwargs["output_mode"] == "debug"


@pytest.mark.asyncio
async def test_orcho_run_threads_session_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    await orcho_run_start(
        task="x", project_dir="/p", mock=True, session_mode="stateless",
    )

    assert fake.spawn_kwargs is not None
    assert fake.spawn_kwargs["session_mode"] == "stateless"


@pytest.mark.asyncio
async def test_orcho_resume_threads_profile_without_mode_arg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    await orcho_run_resume("run123", profile="planning")

    assert fake.resume_kwargs == {"run_id": "run123", "profile": "planning"}


# ── start: validation → InvalidPlanError ──────────────────────────────


@pytest.mark.asyncio
async def test_start_run_rejects_both_task_and_task_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    with pytest.raises(InvalidPlanError):
        await start_run(task="x", task_file="f.md", project_dir="/p", mock=True)
    # Validation happens before the supervisor is ever asked to spawn.
    assert fake.spawn_kwargs is None


@pytest.mark.asyncio
async def test_start_run_rejects_neither_task_nor_task_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    with pytest.raises(InvalidPlanError):
        await start_run(project_dir="/p", mock=True)
    assert fake.spawn_kwargs is None


# ── start: typed-error passthrough + bare-leak translation ────────────


@pytest.mark.asyncio
async def test_start_run_passes_through_pipeline_spawn_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSupervisor(
        tmp_path, spawn_exc=PipelineSpawnError("at capacity"),
    )
    _patch_supervisor(monkeypatch, fake)

    with pytest.raises(PipelineSpawnError, match="at capacity"):
        await start_run(task="x", project_dir="/p", mock=True)


@pytest.mark.asyncio
async def test_start_run_passes_through_workspace_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSupervisor(
        tmp_path, spawn_exc=WorkspaceNotResolvedError("no workspace"),
    )
    _patch_supervisor(monkeypatch, fake)

    with pytest.raises(WorkspaceNotResolvedError, match="no workspace"):
        await start_run(task="x", project_dir="/p", mock=True)


@pytest.mark.asyncio
async def test_start_run_translates_bare_leak_to_pipeline_spawn_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-OrchoMCPError leak from the supervisor becomes the typed
    supervisor/subprocess error, never an untyped exception."""
    fake = _FakeSupervisor(tmp_path, spawn_exc=RuntimeError("popen exploded"))
    _patch_supervisor(monkeypatch, fake)

    with pytest.raises(PipelineSpawnError, match="popen exploded"):
        await start_run(task="x", project_dir="/p", mock=True)


# ── resume: typed-error passthrough ───────────────────────────────────


@pytest.mark.asyncio
async def test_resume_run_passes_through_run_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSupervisor(
        tmp_path, resume_exc=RunNotFoundError("run not found: r1"),
    )
    _patch_supervisor(monkeypatch, fake)

    with pytest.raises(RunNotFoundError, match="run not found: r1"):
        await resume_run("r1")


@pytest.mark.asyncio
async def test_resume_run_translates_bare_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSupervisor(tmp_path, resume_exc=OSError("disk gone"))
    _patch_supervisor(monkeypatch, fake)

    with pytest.raises(PipelineSpawnError, match="disk gone"):
        await resume_run("r1")


# ── cancel: success / pending status preserved ────────────────────────


@pytest.mark.parametrize(
    "status",
    [
        "signal_sent(graceful)",
        "signal_sent(hard)",
        "already_dead",
        "already_done",
    ],
)
@pytest.mark.asyncio
async def test_cancel_run_preserves_status_text(
    status: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSupervisor(tmp_path, cancel_status=status)
    _patch_supervisor(monkeypatch, fake)

    result = await cancel_run("r1", mode="graceful")

    assert result.run_id == "r1"
    assert result.status == status


# ── cancel: invalid mode ValueError → InvalidPlanError (bugfix) ───────


@pytest.mark.asyncio
async def test_cancel_run_invalid_mode_is_invalid_plan_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bare ValueError the supervisor raises for an unknown cancel
    mode must be translated at the run-control boundary instead of
    leaking as an untyped Python exception."""
    fake = _FakeSupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    with pytest.raises(InvalidPlanError, match="cancel mode must be"):
        await cancel_run("r1", mode="bogus")


@pytest.mark.asyncio
async def test_cancel_run_passes_through_run_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSupervisor(
        tmp_path, cancel_exc=RunNotFoundError("run r1: no state file"),
    )
    _patch_supervisor(monkeypatch, fake)

    with pytest.raises(RunNotFoundError, match="no state file"):
        await cancel_run("r1", mode="hard")
