"""Unit tests for the async / offload variant of the typed silent
boundary pilot — ``orcho_run_project_typed_async`` and the
``start_project_typed_silent_async`` adapter.

The async path's load-bearing contract:

  1. **Returns immediately.** The handler resolves before the
     pipeline body completes — the run executes in
     ``asyncio.to_thread``.
  2. **run_id is workspace-resolvable.** The response carries a
     ``run_id`` that matches a directory name under
     ``<workspace>/runspace/runs/``. ``orcho_run_status(run_id)``
     finds the run through the standard SDK walk.
  3. **ORCHO_RUN_ID env var threads through.** Inside the worker
     thread the env var equals ``run_id`` so orcho-core's
     ``resolve_run_id_and_setup_logging`` adopts it; the env var is
     restored on exit so successive runs don't inherit stale state.
  4. **SILENT contract preserved.** No stdout / stderr leaks during
     start or completion; the file sinks land as they would for the
     blocking sibling.
  5. **Task lifecycle.** The background ``asyncio.Task`` is held in
     ``_active_pilot_tasks`` while running and removed via the
     done-callback once it settles.

End-to-end flow exercises the real ``MockAgentProvider`` ->
``run_project_pipeline`` path through the adapter, then asserts
the standard read-path (``orcho_run_status`` /
``orcho_run_events_tail``) resolves the resulting run.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from orcho_mcp.errors import (
    InvalidPlanError,
    WorkspaceNotResolvedError,
)
from orcho_mcp.run_control.typed_pilot import (
    _active_pilot_tasks,
    start_project_typed_silent_async,
)
from orcho_mcp.schemas import TypedRunStartedResult
from orcho_mcp.tools import (
    orcho_run_events_tail,
    orcho_run_project_typed_async,
    orcho_run_status,
)

# ── shared fixtures ───────────────────────────────────────────────────


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Minimal initialised git checkout — pipeline reads git state on
    some phases; ``task`` profile is the minimal path."""
    from tests.conftest import init_git_repo
    project = tmp_path / "proj"
    init_git_repo(project)
    return project


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Workspace with ``<ws>/runspace/runs/`` provisioned — matches
    the ``fake_workspace`` layout the rest of the read-tool tests
    use. ``$ORCHO_WORKSPACE`` is set so the SDK + supervisor path
    resolver agree on the runs dir.

    ``ORCHO_RUNSPACE`` is cleared for the same reason ``fake_workspace``
    clears it: ``runspace_dir()`` / ``get_runs_dir()`` prefer
    ``$ORCHO_RUNSPACE`` over ``$ORCHO_WORKSPACE/runspace``. When this
    suite runs inside an ambient Orcho run that var points at the real
    runspace, so without this delenv the pilot's ``output_dir`` resolves
    against the real tree instead of this tmp workspace.
    """
    ws = tmp_path / "ws"
    (ws / "runspace" / "runs").mkdir(parents=True)
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
    return ws


# ── 1. start contract ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_start_returns_immediately_with_running_status(
    workspace: Path,
    project_dir: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """The handler resolves with ``status="running"`` before the
    background task finishes. Proves the offload shape: caller can
    proceed to polling instead of blocking on the pipeline."""
    result = await start_project_typed_silent_async(
        task="async start contract",
        project_dir=str(project_dir),
    )

    assert isinstance(result, TypedRunStartedResult)
    assert result.status == "running"
    assert result.run_id
    assert result.output_dir.startswith(str(workspace / "runspace" / "runs"))
    assert Path(result.output_dir).name == result.run_id
    assert result.started_at  # ISO timestamp populated

    # The bg task is registered and still runnable.
    assert result.run_id in _active_pilot_tasks
    task = _active_pilot_tasks[result.run_id]

    # No stdout / stderr leak at start time.
    out = capsys.readouterr()
    assert out.out == "", f"start leaked stdout: {out.out!r}"
    assert out.err == "", f"start leaked stderr: {out.err!r}"

    # Drain the background task so we don't leave it dangling for
    # the next test in this module — pytest's event-loop teardown
    # would warn about pending tasks otherwise.
    await asyncio.wait_for(task, timeout=10)


@pytest.mark.asyncio
async def test_async_start_requires_workspace(
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a resolvable workspace the start handler raises
    ``WorkspaceNotResolvedError`` — the async path is workspace-
    aware by design (run_id needs to land under
    ``<workspace>/runspace/runs/`` so the read tools can find it)."""
    monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
    # Also clear the ambient runspace pointer — otherwise an Orcho-run
    # ``$ORCHO_RUNSPACE`` resolves a runs dir and no error is raised.
    monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
    # Make sure walk-up also can't find anything by chdir to a
    # neutral location.
    monkeypatch.chdir("/tmp")
    with pytest.raises(WorkspaceNotResolvedError):
        await start_project_typed_silent_async(
            task="t",
            project_dir=str(project_dir),
        )


@pytest.mark.asyncio
async def test_async_start_rejects_real_provider() -> None:
    """``mock=False`` is rejected at the boundary — same pilot
    constraint as the blocking sibling, surfaced before any task is
    spawned."""
    with pytest.raises(InvalidPlanError, match="mock-only"):
        await start_project_typed_silent_async(
            task="t",
            project_dir="/p",
            mock=False,
        )


@pytest.mark.asyncio
async def test_async_start_rejects_empty_task_and_project() -> None:
    """Required-string guards mirror the blocking sibling so an
    empty value can't slip into the background task and become a
    silent failure."""
    with pytest.raises(InvalidPlanError, match="'task'"):
        await start_project_typed_silent_async(
            task="", project_dir="/p",
        )
    with pytest.raises(InvalidPlanError, match="'project_dir'"):
        await start_project_typed_silent_async(
            task="t", project_dir="",
        )


# ── 2. end-to-end: start → background → workspace read tools ─────────


@pytest.mark.asyncio
async def test_async_run_completes_and_is_findable_via_read_tools(
    workspace: Path,
    project_dir: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """End-to-end: drive a real mock run through the async tool, wait
    for the background task, then verify that
    ``orcho_run_status(run_id)`` and ``orcho_run_events_tail(run_id)``
    resolve via the standard workspace-aware path. This is the
    integration that proves the async tool's
    ``run_id`` is a first-class identifier the existing read surface
    consumes — no new polling endpoint required."""
    started = await orcho_run_project_typed_async(
        task="async pilot e2e smoke",
        project_dir=str(project_dir),
    )
    assert started.status == "running"

    # Wait for the background task to settle.
    task = _active_pilot_tasks.get(started.run_id)
    assert task is not None, "task should still be in flight at this point"
    await asyncio.wait_for(task, timeout=15)
    # Done-callback should have cleaned up the registry entry.
    assert started.run_id not in _active_pilot_tasks

    # No stdout / stderr leak across the whole lifecycle.
    out = capsys.readouterr()
    assert out.out == "", f"async lifecycle leaked stdout: {out.out!r}"
    assert out.err == "", f"async lifecycle leaked stderr: {out.err!r}"

    # ✓ meta.json landed at the workspace-derived path.
    run_dir = workspace / "runspace" / "runs" / started.run_id
    assert run_dir.is_dir(), "background run never created its dir"
    meta_path = run_dir / "meta.json"
    assert meta_path.is_file()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "done"

    # ✓ The standard read tools resolve the run by ID — proves the
    # async tool integrates with the existing observation surface.
    status = orcho_run_status(started.run_id)
    assert status.meta["status"] == "done"

    events_tail = orcho_run_events_tail(started.run_id, limit=200)
    kinds = [e.kind for e in events_tail.events]
    for spine_kind in ("run.start", "phase.start", "phase.end", "run.end"):
        assert spine_kind in kinds, (
            f"workspace read tool missed spine kind {spine_kind!r}; "
            f"got {kinds!r}"
        )


# ── 3. ORCHO_RUN_ID env var threading ────────────────────────────────


@pytest.mark.asyncio
async def test_orcho_run_id_env_threaded_into_worker_and_restored(
    workspace: Path,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker thread sets ``ORCHO_RUN_ID=<run_id>`` so orcho-core's
    ``resolve_run_id_and_setup_logging`` adopts the caller's id; on
    exit the env is restored to its prior state. This pins the
    contract that makes ``run_id`` == ``output_dir.name``."""
    monkeypatch.setenv("ORCHO_RUN_ID", "sentinel_prior_value")
    started = await start_project_typed_silent_async(
        task="env-thread smoke",
        project_dir=str(project_dir),
    )

    task = _active_pilot_tasks.get(started.run_id)
    assert task is not None
    await asyncio.wait_for(task, timeout=15)

    # ✓ env var restored to the pre-run value (NOT left at run_id).
    # The worker's finally clause must roll back the env after the
    # pipeline returns, otherwise the next async invocation would
    # inherit the previous run's id and core would reuse it.
    assert os.environ.get("ORCHO_RUN_ID") == "sentinel_prior_value"

    # ✓ The run dir landed at the workspace-derived path. This is the
    # observable consequence of run_id minting + env-thread + output_dir
    # composition: orcho-core wrote meta.json + events.jsonl at
    # ``<workspace>/runspace/runs/<our_run_id>/``, which is what the
    # workspace-aware read tools resolve when given ``started.run_id``.
    # (orcho-core's bootstrap derives run_id from ``ORCHO_RUN_ID`` env
    # > minted timestamp; the e2e test pins that the read-tool path
    # resolves end-to-end.)
    run_dir = workspace / "runspace" / "runs" / started.run_id
    assert run_dir.is_dir()
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "done"


@pytest.mark.asyncio
async def test_env_restored_when_no_prior_value(
    workspace: Path,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``ORCHO_RUN_ID`` was unset at start, the worker must remove
    it (not leave it set) so the next test / next run picks up the
    clean state."""
    monkeypatch.delenv("ORCHO_RUN_ID", raising=False)
    started = await start_project_typed_silent_async(
        task="env-restore-unset smoke",
        project_dir=str(project_dir),
    )
    task = _active_pilot_tasks.get(started.run_id)
    assert task is not None
    await asyncio.wait_for(task, timeout=15)
    assert "ORCHO_RUN_ID" not in os.environ


# ── 4. background-task lifecycle ─────────────────────────────────────


@pytest.mark.asyncio
async def test_failed_background_run_cleans_up_registry(
    workspace: Path,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the background pipeline raises, the done-callback still
    drains the registry entry so subsequent calls aren't blocked by a
    stale task reference. The exception is logged (not surfaced as a
    second crash) — the file sinks remain the durable record."""
    # Force ``run_project_pipeline`` to raise from inside the worker.
    # The adapter routes through ``RunService.start``, whose default
    # lazy-imports ``pipeline.project.app.run_project_pipeline`` per
    # call — so patch the source name, not the adapter module attribute.
    def _boom(_request):
        raise RuntimeError("scripted background failure")

    monkeypatch.setattr(
        "pipeline.project.app.run_project_pipeline", _boom,
    )

    started = await start_project_typed_silent_async(
        task="failure-path smoke",
        project_dir=str(project_dir),
    )

    task = _active_pilot_tasks.get(started.run_id)
    assert task is not None

    # Awaiting the task surfaces the exception; pytest.raises pins
    # that the exception didn't get silently swallowed before the
    # callback ran.
    with pytest.raises(RuntimeError, match="scripted background failure"):
        await task

    # Registry cleaned up via done-callback.
    assert started.run_id not in _active_pilot_tasks


# ── 5. tool handler delegates without inspection ─────────────────────


@pytest.mark.asyncio
async def test_async_tool_handler_delegates_to_adapter(
    workspace: Path,
    project_dir: Path,
) -> None:
    """The @mcp.tool async handler is a thin one-line delegation;
    calling it as a plain coroutine flows through to the adapter and
    returns the typed start model."""
    result = await orcho_run_project_typed_async(
        task="handler delegation smoke",
        project_dir=str(project_dir),
    )
    assert isinstance(result, TypedRunStartedResult)
    assert result.status == "running"

    # Drain background task to keep the next test clean.
    task = _active_pilot_tasks.get(result.run_id)
    assert task is not None
    await asyncio.wait_for(task, timeout=15)


# ── 6. _collect_event_kinds — raw output_dir, no workspace resolution ────
#
# Stage-5 no-change guard. ``_collect_event_kinds`` parses ``events.jsonl``
# directly from a caller-chosen ``output_dir`` that may live outside any
# managed workspace (tmp / fixture paths). ``sdk.run_control`` read helpers
# resolve a run by ``run_id`` + workspace via ``find_run``, so they are
# inapplicable here — this surface stays a raw, workspace-free file read.


def test_collect_event_kinds_reads_raw_dir_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parses kinds from a loose dir with NO ``$ORCHO_WORKSPACE`` and no
    ``runspace/runs`` ancestry — proving the read is workspace-free and
    cannot be replaced by a run_id+workspace-resolving SDK call."""
    from orcho_mcp.run_control.typed_pilot import _collect_event_kinds

    # No managed workspace at all: a run_id+workspace SDK lookup would fail.
    monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
    monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)

    loose_dir = tmp_path / "loose_run"  # not under any runspace/runs tree
    loose_dir.mkdir()
    (loose_dir / "events.jsonl").write_text(
        "\n".join(
            json.dumps(e)
            for e in (
                {"seq": 1, "kind": "run.start", "phase": None, "payload": {}},
                {"seq": 2, "kind": "phase.start", "phase": "plan", "payload": {}},
                {"seq": 3, "kind": "run.end", "phase": None, "payload": {}},
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert _collect_event_kinds(loose_dir) == [
        "run.start", "phase.start", "run.end",
    ]


def test_collect_event_kinds_missing_file_is_empty(tmp_path: Path) -> None:
    """No ``events.jsonl`` → empty list (best-effort spine read, never
    raises)."""
    from orcho_mcp.run_control.typed_pilot import _collect_event_kinds

    assert _collect_event_kinds(tmp_path) == []
    assert _collect_event_kinds(None) == []
