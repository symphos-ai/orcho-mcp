"""orcho_mcp.supervisor.spawn — ``execute`` for new-run spawning.

Builds the orchestrator argv via ``sdk.build_orch_argv``, launches the
pipeline subprocess in its own session (so SIGKILL on the pgid reaches
the whole tree), persists the supervisor state file, and schedules a
background ``_reap`` task. Per-project asyncio lock serialises
concurrent spawns on the same ``project_dir`` so two runs cannot race
on a shared checkpoint store mid-resume.

Composed into ``RunsSupervisor`` via a thin delegation method in
``manager.py``; this module exports the operation as a top-level
function that takes the supervisor as its first argument. The function
reads ``sup._runs``, ``sup._project_locks``, ``sup._max_runs`` directly
and schedules ``sup._reap`` (the lifecycle delegation method) for
post-mortem.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from typing import TYPE_CHECKING

from core.observability.logging import normalize_output_mode
from sdk import build_orch_argv

# ``auto-detect`` is a run-start *selector token* consumed by orcho-core's
# CLI before any profile resolution — NOT a registered profile name. Import
# the canonical token DEFENSIVELY so a stale core that predates the selector
# still loads (falling back to the literal); this keeps the env-guard below
# comparing against a single source of truth instead of a duplicated literal.
try:
    from pipeline.project.auto_detect import (
        AUTO_DETECT_PROFILE_TOKEN as _AUTO_DETECT_PROFILE_TOKEN,
    )
except ImportError:  # pragma: no cover - exercised by the stale-core unit test
    _AUTO_DETECT_PROFILE_TOKEN = "auto-detect"

from orcho_mcp.errors import PipelineSpawnError
from orcho_mcp.supervisor.handle import RunHandle
from orcho_mcp.supervisor.paths import (
    resolve_project_dir,
    resolve_runs_dir,
    resolve_task_file,
    workspace_from_runs_dir,
)
from orcho_mcp.supervisor.state import now_iso, write_state

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

    output_mode = normalize_output_mode(output_mode)
    project_dir = resolve_project_dir(project_dir)
    task_file = resolve_task_file(task_file, project_dir=project_dir)
    runs_dir = resolve_runs_dir()
    run_id = sup.mint_run_id()
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)

    argv = build_orch_argv(
        project=project_dir,
        task=task,
        task_file=task_file,
        workspace=workspace_from_runs_dir(runs_dir),
        run_id=run_id,
        output_dir=str(run_dir),
        mock=mock,
        max_rounds=max_rounds,
        mock_validate_plan_reject=mock_validate_plan_reject,
        output_mode=output_mode,
        session_mode=session_mode,
        profile=profile,
        attach=attach,
        attach_text=attach_text,
        attach_image=attach_image,
        attach_binary=attach_binary,
        from_run_plan=from_run_plan,
    )
    cmd = [sys.executable, "-m", "pipeline.project_orchestrator", *argv]

    env = os.environ.copy()
    env["ORCHO_RUN_ID"] = run_id
    # ``--profile`` argv flag is the active profile selection
    # surface. ``ORCHO_PIPELINE`` env var is still honoured by
    # orcho-core as an explicit override (e.g. for testing custom
    # profiles without changing supervisor call shape) — set when
    # the caller passed a non-default profile.
    #
    # The ``auto-detect`` selector is the exception: it is a token the
    # CLI resolves into a concrete profile + mode *before* any profile
    # registry lookup, and it routes ONLY through argv ``--profile``.
    # ``ORCHO_PIPELINE`` is a concrete-profile override that feeds
    # straight into ``_resolve_profile_name`` / the profile registry, so
    # setting it to the selector token would pre-resolve (and break) the
    # registry lookup. Keep the selector out of the env override.
    #
    # ``env`` is a copy of ``os.environ``. If the MCP server inherited
    # ORCHO_PIPELINE, the subprocess would receive that concrete-profile
    # override alongside argv ``--profile auto-detect`` and could run a
    # different profile than the one recorded in ``meta.auto_detect``.
    # Drop the override entirely for the selector path so the selected
    # profile is owned by core's auto-detect decision.
    if profile == _AUTO_DETECT_PROFILE_TOKEN:
        env.pop("ORCHO_PIPELINE", None)
    elif profile and profile != "feature":
        env["ORCHO_PIPELINE"] = profile

    runner_log = run_dir / "runner.log"

    # Use per-project asyncio.Lock to serialise concurrent spawns on the
    # same project_dir. This avoids two runs racing on the same checkpoint
    # store mid-resume.
    lock = sup._project_locks.setdefault(project_dir, asyncio.Lock())
    async with lock:
        try:
            log_fd = runner_log.open("w", encoding="utf-8")
            popen = subprocess.Popen(
                cmd,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                cwd=project_dir,
                env=env,
                start_new_session=True,
            )
        except (OSError, FileNotFoundError) as e:
            raise PipelineSpawnError(
                f"failed to spawn pipeline subprocess: {e}"
            ) from e

    # ``start_new_session=True`` makes the child a session leader, so
    # its pgid equals its pid. Capture both for clarity.
    handle = RunHandle(
        run_id=run_id,
        pid=popen.pid,
        pgid=popen.pid,
        run_dir=run_dir,
        project_dir=project_dir,
        command=cmd,
        started_at=now_iso(),
        progress_token=progress_token,
        mock=mock,
        output_mode=output_mode,
        popen=popen,
    )
    write_state(handle)
    sup._runs[run_id] = handle

    # Reap in the background; updates state on exit.
    asyncio.create_task(sup._reap(handle))
    return handle


__all__ = ["execute"]
