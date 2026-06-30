"""orcho_mcp.supervisor.resume — ``execute`` for ``--resume`` continuation.

Spawns a new subprocess that loads the existing checkpoint via the
orchestrator's ``--resume`` flag. Inherits ``mock`` / ``output_mode``
from the persisted supervisor state file so a paused mock run does
not silently switch providers on resume. Profile resolves to
explicit-caller-override → ``meta.profile`` → ``"feature"`` fallback
(the semantic default work kind), matching the CLI resume path.

Composed into ``RunsSupervisor`` via a thin delegation method in
``manager.py``; this module exports the operation as a top-level
function that takes the supervisor as its first argument.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from typing import TYPE_CHECKING

from core.observability.logging import normalize_output_mode
from sdk import build_orch_argv

from orcho_mcp.errors import (
    PipelineSpawnError,
    RunNotFoundError,
)
from orcho_mcp.supervisor.handle import RunHandle
from orcho_mcp.supervisor.paths import (
    resolve_runs_dir,
    workspace_from_runs_dir,
)
from orcho_mcp.supervisor.state import (
    now_iso,
    read_meta_profile,
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
            the plan only.
    """
    runs_dir = resolve_runs_dir()
    run_dir = runs_dir / run_id
    if not run_dir.is_dir():
        raise RunNotFoundError(f"run not found: {run_id} (in {runs_dir})")

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

    # orcho-core's resume_context falls back to ``meta["task"]`` when
    # ``--task`` is absent (see ``pipeline.control.resume_context.
    # resolve_task``). The supervisor deliberately omits ``--task``
    # here so the CLI classifies the spawn as
    # ``ResumeMode.CHECKPOINT`` (resume + no task) rather than
    # ``FOLLOWUP`` (resume + task). CHECKPOINT mode re-uses the
    # parent run dir and hydrates the checkpoint store; FOLLOWUP
    # would try to mint a new run inside the same dir and collide
    # on the run-id-already-exists check.
    #
    # We still validate the parent has a recorded task so we can
    # surface a structured error when the persisted meta is
    # missing it (pre-write kill, corruption).
    if not read_meta_task(run_dir):
        raise RunNotFoundError(
            f"run {run_id}: meta.json missing 'task' — resume cannot "
            "synthesise the orchestrator argv. (Run was likely killed "
            "before the pipeline wrote initial meta.json.)"
        )

    # Resolve effective profile:
    #  * explicit profile (caller passed) — wins, treated as a
    #    deliberate profile switch;
    #  * else inherit ``meta.profile`` from the original run;
    #  * else fall back to the semantic default ``"feature"`` for
    #    runs whose meta does not record a profile. The fallback
    #    mirrors ``pipeline.control.resume_context.
    #    resolve_resume_profile`` on the orcho-core side so CLI and
    #    MCP resume paths agree.
    if profile is None or not profile.strip():
        effective_profile = read_meta_profile(run_dir) or "feature"
    else:
        effective_profile = profile

    # Preserve original launch options that affect subprocess behaviour.
    # Without preserving ``--mock``, a paused mock run resumed via
    # approve+resume would spawn a non-mock subprocess that immediately
    # tries to invoke the real provider CLI on its first review/build
    # call and crash. ``output_mode`` is similarly user-visible: resumed
    # runs should keep the same transcript verbosity as their initial
    # spawn.
    #
    # NOTE — narrower than the ideal resume contract. Others
    # (``max_rounds``, ``attach*``, per-phase model/provider
    # overrides) currently flow only through whatever orcho-core's
    # ``--resume`` recovers from checkpoint + meta.json, plus the
    # inherited process environment. A broader audit of the resume
    # launch contract is a separate hardening task.
    # Counters that are intentionally NOT re-applied:
    #   * ``mock_validate_plan_reject`` — one-shot rejection counter,
    #     spent by the time the pause fired.
    original_mock = bool(state.get("mock", False))
    # Validate persisted mode defensively: a corrupted supervisor.json
    # field shouldn't block a resume — fall back to summary.
    try:
        original_output_mode = normalize_output_mode(
            state.get("output_mode") or "summary"
        )
    except ValueError:
        original_output_mode = "summary"
    argv = build_orch_argv(
        project=project_dir,
        workspace=workspace_from_runs_dir(runs_dir),
        resume=run_id,
        run_id=run_id,
        output_dir=str(run_dir),
        profile=effective_profile,
        mock=original_mock,
        output_mode=original_output_mode,
    )
    cmd = [sys.executable, "-m", "pipeline.project_orchestrator", *argv]

    env = os.environ.copy()
    env["ORCHO_RUN_ID"] = run_id

    runner_log = run_dir / "runner.log"
    log_fd = runner_log.open("a", encoding="utf-8")
    log_fd.write(f"\n=== resume @ {now_iso()} ===\n")
    log_fd.flush()

    try:
        popen = subprocess.Popen(
            cmd,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            cwd=project_dir,
            env=env,
            start_new_session=True,
        )
    except (OSError, FileNotFoundError) as e:
        raise PipelineSpawnError(f"failed to resume {run_id}: {e}") from e

    handle = RunHandle(
        run_id=run_id,
        pid=popen.pid,
        pgid=popen.pid,
        run_dir=run_dir,
        project_dir=project_dir,
        command=cmd,
        started_at=now_iso(),
        mock=original_mock,
        output_mode=original_output_mode,
        popen=popen,
        status="running",
    )
    write_state(handle)
    sup._runs[run_id] = handle
    asyncio.create_task(sup._reap(handle))
    return handle


__all__ = ["execute"]
