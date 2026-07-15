"""MCP ownership wrapper for a core detached correction follow-up launch."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from orcho_mcp.errors import PipelineSpawnError
from orcho_mcp.supervisor.handle import RunHandle
from orcho_mcp.supervisor.paths import resolve_runs_dir
from orcho_mcp.supervisor.state import write_state

if TYPE_CHECKING:
    from orcho_mcp.supervisor.manager import RunsSupervisor


async def execute(
    sup: RunsSupervisor, *, parent_run_id: str, operator_comment: str,
) -> RunHandle:
    """Launch and register a correction child without taking over its parent.

    Core owns the correction argv, durable continuity, and context artifact.
    MCP only makes the newly-created child controllable by recording its
    standard supervisor state and scheduling the normal reaper.
    """
    if not operator_comment.strip():
        raise PipelineSpawnError("operator_comment must be non-empty")
    active = [handle for handle in sup._runs.values() if handle.status == "running"]
    if len(active) >= sup._max_runs:
        raise PipelineSpawnError(f"max concurrent runs reached ({sup._max_runs})")
    runs_dir = resolve_runs_dir()
    # Keep the supervisor package importable for a stable core that predates
    # correction follow-up. Only an actual correction launch needs this seam.
    from sdk.errors import LaunchError
    from sdk.run_control.launch import (
        CorrectionFollowupLaunchRequest,
        launch_correction_followup,
    )
    try:
        result = launch_correction_followup(
            CorrectionFollowupLaunchRequest(
                parent_run_id=parent_run_id,
                operator_comment=operator_comment.strip(),
                runs_dir=str(runs_dir),
            ),
            run_id=sup.mint_run_id(),
        )
    except LaunchError as exc:
        raise PipelineSpawnError(str(exc)) from exc

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
    )
    write_state(handle)
    sup._runs[handle.run_id] = handle
    asyncio.create_task(sup._reap(handle))
    return handle


__all__ = ["execute"]
