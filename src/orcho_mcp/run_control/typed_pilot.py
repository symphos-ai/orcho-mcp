"""Typed silent boundary pilot — call ``run_project_pipeline`` directly.

Adapter that drives a single-project run as an in-process library call
through the orcho-core typed silent boundary:

    run_project_pipeline(
        ProjectRunRequest(
            ...,
            presentation=PresentationPolicy.SILENT,
            no_interactive=True,
        )
    )

Companion to orcho-core's
``docs/examples/typed_boundary_consumer.md``. The wider MCP surface
(``orcho_run_start`` etc.) keeps its supervisor + subprocess shape;
this pilot proves the alternative library-call shape with one focused
tool, scoped to mock-provider runs only.

Why a separate tool and not a replacement:

  * ``orcho_run_start`` is designed for long-running real-provider
    runs — return run_id immediately, stream progress over
    ``progressToken``, cancel via signal. That shape requires the
    background subprocess.
  * ``orcho_run_project_typed`` is a foreground blocking call — the
    handler waits until the pipeline completes and returns the result
    in one round-trip. Best suited for short flows like mock
    smoke-checks, fixture-driven plan generation, or any
    integration test that wants a deterministic structured result
    without async polling.

The adapter is the ONE place that:

  * constructs the typed :class:`pipeline.project.types.ProjectRunRequest`,
  * pins ``presentation=PresentationPolicy.SILENT`` and
    ``no_interactive=True``,
  * supplies a :class:`agents.runtimes.MockAgentProvider`,
  * reads the canonical event-spine ``kind`` values from
    ``events.jsonl`` for the wire response,
  * packs the result into :class:`orcho_mcp.schemas.TypedRunResult`.

No stdout is captured, parsed, or inspected anywhere in this module.
Status comes from ``ProjectRunResult.session`` (the in-memory mirror
of the persisted ``meta.json``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents.runtimes import MockAgentProvider
from pipeline.project.types import (
    PresentationPolicy,
    ProjectRunRequest,
)

from orcho_mcp.errors import (
    InvalidPlanError,
    WorkspaceNotResolvedError,
)
from orcho_mcp.schemas import TypedRunResult, TypedRunStartedResult

logger = logging.getLogger(__name__)

# ── async offload bookkeeping ─────────────────────────────────────────
#
# Background pipeline tasks need a strong reference to survive past
# the synchronous return of the start handler; without one,
# ``asyncio.create_task`` returns a task that may be garbage-collected
# before completion. The dict holds the reference until the task
# settles, then the ``add_done_callback`` cleans up.
_active_pilot_tasks: dict[str, asyncio.Task] = {}

# ``ORCHO_RUN_ID`` is a process-wide env var that orcho-core consults
# in ``resolve_run_id_and_setup_logging`` (priority: ``resume_from``
# > ``$ORCHO_RUN_ID`` > minted timestamp). The async pilot writes it
# inside the worker thread so each background run picks up its own
# id. The lock serialises start-of-run env writes so two concurrent
# starts don't race — the env restore inside the worker is also
# guarded so an interleaved second start can't observe the
# half-restored state of the first. Pilot scope: real concurrency
# (e.g. parallel async runs) would replace this with a
# per-invocation contextvar in core; until then the lock keeps the
# start-and-restore window honest.
_pilot_env_lock = threading.Lock()


def run_project_typed_silent(
    *,
    task: str,
    project_dir: str,
    output_dir: str,
    profile: str = "task",
    mock: bool = True,
    max_rounds: int = 1,
) -> TypedRunResult:
    """Drive a single-project pipeline run via the typed silent boundary.

    See the ``orcho_run_project_typed`` MCP tool docstring (in
    ``orcho_mcp.tools``) for the wire contract. This module owns the
    implementation; the tool handler is a thin shim.

    The default ``profile="task"`` is deliberately retained: it is an
    internal scoped profile (not a public semantic work-kind choice),
    kept here only because it is the leanest mock-completable shape for
    this pilot. Real runs select a semantic profile via
    ``orcho_run_start``.
    """
    if not mock:
        # Pilot scope. Real-provider runs go through ``orcho_run_start``
        # so the MCP client can stream progress + cancel. Removing this
        # guard later is a deliberate scope expansion, not a default.
        raise InvalidPlanError(
            "orcho_run_project_typed is currently mock-only "
            "(pilot scope). For real-provider runs use "
            "orcho_run_start so the MCP client can stream progress "
            "and cancel via signal."
        )
    if not task or not task.strip():
        raise InvalidPlanError(
            "orcho_run_project_typed requires a non-empty 'task'"
        )
    if not project_dir or not project_dir.strip():
        raise InvalidPlanError(
            "orcho_run_project_typed requires a non-empty 'project_dir'"
        )
    if not output_dir or not output_dir.strip():
        raise InvalidPlanError(
            "orcho_run_project_typed requires a non-empty 'output_dir'"
        )

    run_dir = Path(output_dir).expanduser()
    request = ProjectRunRequest(
        task=task,
        project_dir=project_dir,
        output_dir=run_dir,
        max_rounds=max_rounds,
        profile_name=profile,
        provider=MockAgentProvider(latency=0.0),
        presentation=PresentationPolicy.SILENT,
        no_interactive=True,  # post-init invariant: SILENT implies no_interactive.
    )

    # Route the typed run through orcho-core's headless run-control
    # facade. ``RunService.start`` is a pure isinstance dispatcher:
    # a ``ProjectRunRequest`` lands on the same
    # ``pipeline.project.app.run_project_pipeline`` this module called
    # directly before, returning the identical ``ProjectRunResult`` —
    # so the wire result is byte-for-byte unchanged. The constructor is
    # side-effect free (it only stores callables; the real pipeline
    # entrypoint is lazy-imported inside ``start``), so a fresh
    # per-call instance is cheap and keeps the adapter stateless.
    from sdk.run_control import RunService

    result = RunService().start(request)
    return _pack_typed_result(result)


def _pack_typed_result(result: Any) -> TypedRunResult:
    """Convert :class:`pipeline.project.types.ProjectRunResult` into the
    compact :class:`TypedRunResult` wire model.

    Reads status + halt_reason from the in-memory session (the
    persisted ``meta.json`` mirror) and the ordered event kinds from
    ``events.jsonl``. The full surface (``meta``, ``metrics``, raw
    events) stays available through the existing read tools using the
    returned ``run_id``.
    """
    session: dict[str, Any] = dict(result.session or {})
    status = str(session.get("status", ""))
    halt_reason_raw = session.get("halt_reason")
    halt_reason = (
        str(halt_reason_raw) if isinstance(halt_reason_raw, str) else None
    )
    out_dir = result.output_dir
    return TypedRunResult(
        run_id=str(result.run_id or ""),
        output_dir=str(out_dir) if out_dir else "",
        status=status,
        halt_reason=halt_reason,
        event_kinds=_collect_event_kinds(out_dir),
    )


def _collect_event_kinds(run_dir: Path | None) -> list[str]:
    """Return the ordered list of event ``kind`` values from
    ``events.jsonl``.

    Used to prove the canonical run.start → phase.* → run.end spine
    landed under SILENT. NOT a general events-tail reader — full event
    payloads are exposed via the existing ``orcho_run_events_tail``
    tool backed by ``sdk.list_events``. The pilot's output_dir lives
    under tmp / caller-chosen paths that may not be inside a managed
    workspace, so workspace-resolving SDK calls don't apply here.
    """
    if run_dir is None:
        return []
    events_path = Path(run_dir) / "events.jsonl"
    if not events_path.is_file():
        return []
    kinds: list[str] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # A truncated tail line should not crash the pilot — the
            # spine check is best-effort here.
            continue
        kind = entry.get("kind")
        if isinstance(kind, str):
            kinds.append(kind)
    return kinds


def _mint_run_id() -> str:
    """Timestamp-based run_id matching orcho-core's session_ts shape."""
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def _resolve_workspace_runs_dir() -> Path:
    """Locate ``<workspace>/runspace/runs/`` for the current workspace.

    Delegates to the supervisor's path resolver so async pilot runs
    land under the same directory the existing read tools walk.
    Raises :class:`WorkspaceNotResolvedError` if ``$ORCHO_WORKSPACE``
    is unset and walk-up resolution fails — the async path is
    workspace-aware by design, so this is a hard precondition rather
    than a fallback to a synthetic directory.
    """
    from orcho_mcp.supervisor.paths import resolve_runs_dir
    return resolve_runs_dir()


def _run_pipeline_with_env_run_id(
    *,
    task: str,
    project_dir: str,
    output_dir: str,
    profile: str,
    max_rounds: int,
    run_id: str,
) -> None:
    """Worker body executed inside ``asyncio.to_thread``.

    Sets ``ORCHO_RUN_ID`` so the pipeline body adopts the caller's
    run id (matching the directory name we placed under
    ``<workspace>/runspace/runs/``), drives the typed silent
    boundary, then restores the env var. The lock guarantees the
    start-and-restore window is atomic with respect to other async
    pilot runs spawned on the same MCP server process.

    Returns ``None`` — the file sinks own the post-run state, so the
    caller polls ``orcho_run_status(run_id)`` / ``meta.json``
    instead of waiting for a return value. Exceptions propagate to
    the asyncio task so ``add_done_callback`` can surface failures.
    """
    with _pilot_env_lock:
        previous = os.environ.get("ORCHO_RUN_ID")
        os.environ["ORCHO_RUN_ID"] = run_id
    try:
        run_project_typed_silent(
            task=task,
            project_dir=project_dir,
            output_dir=output_dir,
            profile=profile,
            mock=True,
            max_rounds=max_rounds,
        )
    finally:
        with _pilot_env_lock:
            # Only roll back if our value is still in place — an
            # interleaved sibling run may have overwritten it.
            if os.environ.get("ORCHO_RUN_ID") == run_id:
                if previous is None:
                    os.environ.pop("ORCHO_RUN_ID", None)
                else:
                    os.environ["ORCHO_RUN_ID"] = previous


def _log_task_failure(task: asyncio.Task, run_id: str) -> None:
    """Done-callback that drains the task exception (if any) and
    logs it via ``logging`` (NOT ``print`` — stdio purity invariant).

    Without this callback, an unhandled-exception warning would land
    on the asyncio default handler at GC time. The pipeline body
    owns persistence, so a crash here usually means a programming
    error in the adapter or a setup failure (workspace, paths) — the
    log message gives the next caller a breadcrumb when
    ``orcho_run_status(run_id)`` reports a missing meta.json.
    """
    _active_pilot_tasks.pop(run_id, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "async typed pilot run %s raised %s: %s",
            run_id, type(exc).__name__, exc,
        )


async def start_project_typed_silent_async(
    *,
    task: str,
    project_dir: str,
    profile: str = "task",
    mock: bool = True,
    max_rounds: int = 1,
) -> TypedRunStartedResult:
    """Spawn a typed silent run in the background; return immediately.

    See ``orcho_run_project_typed_async`` MCP tool docstring (in
    ``orcho_mcp.tools``) for the wire contract. This module owns the
    implementation; the tool handler is a thin shim.

    Workspace-aware: derives ``output_dir`` under
    ``<workspace>/runspace/runs/<run_id>/`` so the existing
    ``orcho_run_status`` / ``orcho_run_events_tail`` tools resolve
    the run by id through the same SDK path as supervisor-backed
    runs. The blocking sibling
    :func:`run_project_typed_silent` keeps its explicit-``output_dir``
    shape; both can coexist.
    """
    if not mock:
        raise InvalidPlanError(
            "orcho_run_project_typed_async is currently mock-only "
            "(pilot scope). For real-provider runs use "
            "orcho_run_start so the MCP client can stream progress "
            "and cancel via signal."
        )
    if not task or not task.strip():
        raise InvalidPlanError(
            "orcho_run_project_typed_async requires a non-empty 'task'"
        )
    if not project_dir or not project_dir.strip():
        raise InvalidPlanError(
            "orcho_run_project_typed_async requires a non-empty 'project_dir'"
        )

    runs_dir = _resolve_workspace_runs_dir()
    if not runs_dir.is_dir():
        raise WorkspaceNotResolvedError(
            f"workspace runs directory does not exist: {runs_dir}. "
            "Set $ORCHO_WORKSPACE or run from inside an orcho workspace."
        )

    # Mint a run_id that doesn't collide with an existing dir. Same-
    # second collisions are extremely rare under sequential pilot
    # use; if one happens, append a counter rather than sleeping.
    run_id = _mint_run_id()
    if (runs_dir / run_id).exists():
        suffix = 1
        while (runs_dir / f"{run_id}_{suffix}").exists():
            suffix += 1
        run_id = f"{run_id}_{suffix}"

    output_dir = runs_dir / run_id
    started_at = datetime.now(UTC).isoformat()

    task_handle = asyncio.create_task(
        asyncio.to_thread(
            _run_pipeline_with_env_run_id,
            task=task,
            project_dir=project_dir,
            output_dir=str(output_dir),
            profile=profile,
            max_rounds=max_rounds,
            run_id=run_id,
        ),
        name=f"typed-pilot-async-{run_id}",
    )
    _active_pilot_tasks[run_id] = task_handle
    task_handle.add_done_callback(
        lambda t, rid=run_id: _log_task_failure(t, rid)
    )

    return TypedRunStartedResult(
        run_id=run_id,
        output_dir=str(output_dir),
        status="running",
        started_at=started_at,
    )


__all__ = [
    "run_project_typed_silent",
    "start_project_typed_silent_async",
]
