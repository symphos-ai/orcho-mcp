"""orcho_mcp.supervisor.spawn — ``execute`` for new-run spawning.

Delegates the OS mechanics of a detached launch — orchestrator argv
build, ``ORCHO_PIPELINE`` / ``auto-detect`` env handling, run-dir
creation, ``runner.log`` open, and the detached session spawn — to
``sdk.run_control.launch.launch_run``. That is now the single home for
the spawn mechanics; the supervisor keeps only the MCP *policy* around
the one delegated call:

- the capacity gate (``sup._max_runs``);
- the per-project ``asyncio.Lock`` (``sup._project_locks``) that
  serialises concurrent spawns on the same ``project_dir`` so two runs
  cannot race on a shared checkpoint store mid-resume;
- run-id minting (``sup.mint_run_id``);
- wrapping the neutral ``LaunchResult`` into a ``RunHandle``, persisting
  the ``mcp_supervisor.json`` MCP delta, registering the handle, and
  scheduling the background ``_reap`` task.

Composed into ``RunsSupervisor`` via a thin delegation method in
``manager.py``; this module exports the operation as a top-level
function that takes the supervisor as its first argument. The function
reads ``sup._runs``, ``sup._project_locks``, ``sup._max_runs`` directly
and schedules ``sup._reap`` (the lifecycle delegation method) for
post-mortem.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from core.observability.logging import normalize_output_mode
from sdk.errors import LaunchError, NoWorkspace
from sdk.run_control.launch import LaunchSpec, launch_run

from orcho_mcp.errors import (
    PipelineSpawnError,
    WorkspaceNotResolvedError,
)
from orcho_mcp.supervisor.handle import RunHandle
from orcho_mcp.supervisor.paths import (
    resolve_project_dir,
    resolve_runs_dir,
    resolve_task_file,
    workspace_from_runs_dir,
)
from orcho_mcp.supervisor.state import write_state

if TYPE_CHECKING:
    from orcho_mcp.supervisor.manager import RunsSupervisor


async def execute(
    sup: RunsSupervisor,
    *,
    task: str | None = None,
    task_file: str | None = None,
    project_dir: str,
    profile: str = "feature",
    mock: bool = False,
    max_rounds: int | None = None,
    mock_validate_plan_reject: int = 0,
    output_mode: str = "summary",
    session_mode: str = "auto",
    progress_token: str | None = None,
    attach: list[str] | None = None,
    attach_text: list[str] | None = None,
    attach_image: list[str] | None = None,
    attach_binary: list[str] | None = None,
    from_run_plan: str | None = None,
) -> RunHandle:
    """Spawn a new pipeline subprocess. Returns immediately with the handle.

    Args:
        profile: pipeline profile name, keyed by semantic work
            kind. Built-ins include ``feature`` (default),
            ``small_task``, ``complex_feature``, ``planning``,
            ``code_review``, ``refactor``, and ``migration``.
            Custom profiles ship via ``orcho.profiles`` entry
            points. Threaded through the ``--profile`` argv flag.
        from_run_plan: parent run id or absolute path whose
            ``parsed_plan.json`` the child run inherits. When
            supplied the child:
              * loads the parent's parsed plan via the typed
                artefact loader (no markdown re-parse);
              * projects the selected ``profile`` to drop the
                leading plan / validate_plan block — child starts
                at implement with state.parsed_plan already
                hydrated;
              * stamps ``plan_source="run"`` +
                ``plan_source_run_id`` on meta.json for child →
                parent correlation.
            The parent run must contain ``parsed_plan.json`` or
            the spawn fails fast with a clear diagnostic.
            Mutually exclusive with ``--resume`` semantics — this
            surface is for spawning a NEW run that inherits a
            parent's plan; use ``orcho_run_resume`` to continue
            the same run from its checkpoint.

    Raises:
        PipelineSpawnError: capacity exceeded or spawn failed.
        WorkspaceNotResolvedError: runs dir not resolvable.
    """
    active = [h for h in sup._runs.values() if h.status == "running"]
    if len(active) >= sup._max_runs:
        raise PipelineSpawnError(
            f"max concurrent runs reached ({sup._max_runs}). "
            "Cancel a running run or raise ORCHO_MCP_MAX_RUNS."
        )

    # MCP boundary preflight. ``launch_run`` re-validates project_dir /
    # task_file / runs_dir internally, but the supervisor resolves them
    # first so it can fail fast at the MCP boundary — with its richer
    # diagnostics (short task-file names, the project_dir segment-doubling
    # regression) and its ``WorkspaceNotResolvedError`` for an unresolved
    # runs dir — *before* any run directory is created. The resolved
    # absolute paths are then handed to ``LaunchSpec`` so the
    # re-validation inside ``launch_run`` is idempotent and cannot pick a
    # different path than the one the supervisor recorded. Exactly one
    # path (MCP preflight) owns the diagnostics; the argv/env/Popen
    # mechanics live solely inside ``launch_run``.
    output_mode = normalize_output_mode(output_mode)
    project_dir = resolve_project_dir(project_dir)
    task_file = resolve_task_file(task_file, project_dir=project_dir)
    runs_dir = resolve_runs_dir()
    run_id = sup.mint_run_id()

    spec = LaunchSpec(
        project_dir=project_dir,
        task=task,
        task_file=task_file,
        workspace=workspace_from_runs_dir(runs_dir),
        runs_dir=str(runs_dir),
        profile=profile,
        mock=mock,
        max_rounds=max_rounds,
        mock_validate_plan_reject=mock_validate_plan_reject,
        output_mode=output_mode,
        session_mode=session_mode,
        attach=attach,
        attach_text=attach_text,
        attach_image=attach_image,
        attach_binary=attach_binary,
        from_run_plan=from_run_plan,
    )

    # Per-project asyncio.Lock serialises concurrent spawns on the same
    # project_dir. This avoids two runs racing on the same checkpoint
    # store mid-resume. The delegated OS-level spawn runs under the lock.
    lock = sup._project_locks.setdefault(project_dir, asyncio.Lock())
    async with lock:
        try:
            result = launch_run(spec, run_id=run_id)
        except LaunchError as e:
            raise PipelineSpawnError(str(e)) from e
        except NoWorkspace as e:
            raise WorkspaceNotResolvedError(str(e)) from e

    # Wrap the neutral LaunchResult into the MCP-side handle. ``run``
    # carries the durable spawn facts; ``popen`` is the live process the
    # background reaper waits on.
    run = result.run
    handle = RunHandle(
        run_id=run.run_id,
        pid=run.pid,
        pgid=run.pgid,
        run_dir=run.run_dir,
        project_dir=run.project_dir,
        command=run.command,
        started_at=run.started_at,
        progress_token=progress_token,
        mock=run.mock,
        output_mode=run.output_mode,
        popen=result.popen,
    )
    write_state(handle)
    sup._runs[run.run_id] = handle

    # Reap in the background; updates state on exit.
    asyncio.create_task(sup._reap(handle))
    return handle


__all__ = ["execute"]
