"""orcho_mcp.supervisor.resume — ``execute`` for ``--resume`` continuation.

Delegates the detached respawn — orchestrator argv build, env,
``runner.log`` append, and the detached-session spawn — to
``sdk.run_control.resume_run``. That seam reads the neutral
``run_supervisor.json`` (written by ``launch_run`` at spawn) to inherit
``mock`` / ``output_mode`` so a paused mock run does not silently switch
providers on resume, and resolves the effective profile
(explicit-caller-override → ``meta.profile`` → ``"feature"`` fallback,
the semantic default work kind), matching the CLI resume path.

The supervisor keeps only the MCP policy around that single call: the
inspect-only gate (a run this server started carries a durable
``mcp_supervisor.json``; its absence means MCP has no metadata to
respawn a foreign / CLI-started run), wrapping the returned
``LaunchResult`` into a ``RunHandle``, persisting the
``mcp_supervisor.json`` delta, registering the handle, and scheduling
the background ``_reap`` task.

Composed into ``RunsSupervisor`` via a thin delegation method in
``manager.py``; this module exports the operation as a top-level
function that takes the supervisor as its first argument.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sdk.errors import LaunchError, RunNotFound as SdkRunNotFound
from sdk.run_control.launch import resume_run

from orcho_mcp.errors import (
    PipelineSpawnError,
    RunNotFoundError,
)
from orcho_mcp.supervisor.handle import RunHandle
from orcho_mcp.supervisor.paths import resolve_runs_dir
from orcho_mcp.supervisor.state import (
    read_meta_task,
    read_state,
    write_state,
)

if TYPE_CHECKING:
    from orcho_mcp.supervisor.manager import RunsSupervisor


async def execute(
    sup: RunsSupervisor, run_id: str, *, profile: str | None = None,
) -> RunHandle:
    """Spawn a new subprocess that continues an existing run via ``--resume``.

    Used for phase-handoff continuation: pipeline exited rc=4 with
    ``status=awaiting_phase_handoff``, client called
    ``orcho_phase_handoff_decide(..., action="continue" | "retry_feedback"
    | "continue_with_waiver")``,
    and the supervisor spawns a fresh process which loads the
    checkpoint and continues from where it stopped. Also used for
    generic resume after other halt reasons (commit-decision halt,
    manual intervention, etc.).

    Args:
        run_id: target run.
        profile: v2 profile name for the resumed process.
            ``None`` (default): inherit ``meta.profile`` from the
            original run — the desirable behaviour in most cases
            because review/final prompt envelopes depend on the
            active profile (``feature`` carries the full
            plan/validate envelope; internal scoped profiles do
            not). Legacy runs whose ``meta.json`` predates profile
            capture fall back to the semantic default ``"feature"``
            so review/final still get the full prompt envelope.
            Explicit ``profile="<name>"`` is a deliberate profile
            switch — e.g. resume into ``"small_task"`` for a lean
            scoped continuation, or into ``"planning"`` to refine
            the plan only. The ``None`` → ``meta.profile`` →
            ``"feature"`` resolution itself lives in
            ``sdk.run_control.resume_run``; the supervisor forwards
            the caller's value verbatim.
    """
    runs_dir = resolve_runs_dir()
    run_dir = runs_dir / run_id
    if not run_dir.is_dir():
        raise RunNotFoundError(f"run not found: {run_id} (in {runs_dir})")

    # MCP inspect-only gate. A run this server started carries a durable
    # ``mcp_supervisor.json``; its absence means MCP has no supervisor
    # metadata to respawn the run (a foreign / CLI-started run dir), so
    # resume is refused here rather than delegated. Kept MCP-side so the
    # refusal message is stable for the inspect-only contract.
    state = read_state(run_dir)
    if state is None:
        raise RunNotFoundError(
            f"run {run_id}: no mcp_supervisor.json — cannot resume from MCP"
        )
    project_dir = state.get("project_dir") or state.get("cwd")
    if not project_dir:
        raise RunNotFoundError(
            f"run {run_id}: state file missing project_dir"
        )

    # ``resume_run`` also falls back to ``meta["task"]`` when ``--task``
    # is absent (it omits ``--task`` so core classifies the spawn as a
    # CHECKPOINT continuation). Validate the parent has a recorded task
    # MCP-side so the structured missing-task error keeps its stable text
    # when the persisted meta was never written (pre-write kill).
    if not read_meta_task(run_dir):
        raise RunNotFoundError(
            f"run {run_id}: meta.json missing 'task' — resume cannot "
            "synthesise the orchestrator argv. (Run was likely killed "
            "before the pipeline wrote initial meta.json.)"
        )

    # Delegate the detached respawn. ``resume_run`` reads the neutral
    # ``run_supervisor.json`` (written by ``launch_run`` at spawn) to
    # inherit ``mock`` / ``output_mode`` and to resolve the effective
    # profile, then respawns via ``--resume``. The supervisor no longer
    # owns argv/env/Popen mechanics — that is the single SDK home.
    try:
        result = resume_run(run_id, runs_dir=str(runs_dir), profile=profile)
    except SdkRunNotFound as e:
        raise RunNotFoundError(str(e)) from e
    except LaunchError as e:
        raise PipelineSpawnError(f"failed to resume {run_id}: {e}") from e

    # Wrap the neutral LaunchResult into the MCP-side handle. ``mock`` /
    # ``output_mode`` are echoed from ``result.run`` (the inherited values
    # the seam recorded), so a resumed mock run stays a mock run.
    run = result.run
    handle = RunHandle(
        run_id=run.run_id,
        pid=run.pid,
        pgid=run.pgid,
        run_dir=run.run_dir,
        project_dir=run.project_dir,
        command=run.command,
        started_at=run.started_at,
        mock=run.mock,
        output_mode=run.output_mode,
        popen=result.popen,
        status="running",
    )
    write_state(handle)
    sup._runs[run_id] = handle
    asyncio.create_task(sup._reap(handle))
    return handle


__all__ = ["execute"]
