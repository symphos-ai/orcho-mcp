"""orcho_mcp.run_control.lifecycle — run lifecycle service entries.

Async public functions ``start_run`` / ``resume_run`` / ``cancel_run``
back the matching ``orcho_run_start`` / ``orcho_run_resume`` /
``orcho_run_cancel`` MCP tools. Each entry talks to the supervisor
singleton (``orcho_mcp.supervisor.get_supervisor``) and packs the
result into the canonical ``orcho_mcp.schemas`` response model. The
matching @mcp.tool handlers (in ``orcho_mcp.tools``) are one-line
shims that delegate here; docstrings stay in tools.py because they
are the MCP wire contract.

The supervisor import is lazy — done inside each function body, not
at module load — so tests that ``monkeypatch.setattr(
"orcho_mcp.supervisor.get_supervisor", ...)`` continue to substitute
a fake supervisor without having to also patch the captured name on
this module. The original handler bodies in tools.py used the same
lazy-import pattern for the same reason.
"""
from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field, model_validator

from orcho_mcp.errors import (
    InspectOnlyControlError,
    InvalidPlanError,
    OrchoMCPError,
    PipelineSpawnError,
)
from orcho_mcp.schemas import (
    CancelResult,
    CorrectionBlockedResult,
    CorrectionExitResult,
    CorrectionFollowupStartedResult,
    CorrectionOperatorInputRequiredResult,
    InspectOnlyControlResult,
    NextActionRecord,
    ResumeBlockedResult,
    ResumePendingDecisionResult,
    RunResumeResult,
    RunStartedResult,
    RuntimeOverrideArg,
)
from orcho_mcp.services.errors import map_command_errors
from orcho_mcp.services.run_projection import (
    PendingHandoffProjection,
    RunDiagnosisProjection,
    project_pending_handoff,
    project_run_diagnosis,
)


class _CorrectionResumeInput(BaseModel):
    # FastMCP elicitation accepts primitive fields only. Keep this ``str`` at
    # the transport boundary while publishing the same enum through its JSON
    # Schema and validating the closed set below.
    operator_intent: str = Field(json_schema_extra={"enum": ["followup", "exit"]})
    operator_comment: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _require_comment_for_followup(self):
        if self.operator_intent not in {"followup", "exit"}:
            raise ValueError("operator_intent must be followup or exit")
        if self.operator_intent == "followup" and not self.operator_comment:
            raise ValueError("operator_comment is required for followup")
        return self


def _correction_input_result(run_id: str, reason: str) -> CorrectionOperatorInputRequiredResult:
    from orcho_mcp.services.continuation import CORRECTION_RESUME_INPUT_SCHEMA
    return CorrectionOperatorInputRequiredResult(
        run_id=run_id,
        reason=reason,
        next_actions=[NextActionRecord(
            intent="Choose whether to launch the retained-change correction follow-up.",
            tool="orcho_run_resume",
            args={"run_id": run_id},
            optional=False,
            kind="operator_input_required",
            requires_operator_input=True,
            choices=["followup", "exit"],
            input_schema=CORRECTION_RESUME_INPUT_SCHEMA,
        )],
    )


async def _elicit_correction_input(
    ctx: Context | None,
) -> tuple[str, str | None] | None:
    from orcho_mcp.run_control.handoff import _client_supports_form_elicitation

    if ctx is None or not _client_supports_form_elicitation(ctx):
        return None
    result = await ctx.elicit(
        message="Choose whether to launch the correction follow-up.",
        schema=_CorrectionResumeInput,
    )
    if result.action != "accept":
        return None
    return result.data.operator_intent, result.data.operator_comment


def build_inspect_only_control_result(
    run_id: str,
    attempted: Literal["resume", "phase_handoff_decide"],
    *,
    reason: str,
) -> InspectOnlyControlResult:
    """Build the typed ``inspect_only`` refusal payload shared by resume + decide.

    The run was not started by this MCP server (no durable
    ``mcp_supervisor.json`` with a resolvable project_dir), so MCP cannot
    mutate it — it can only inspect it. The CLI-control instruction rides ONLY
    in the free-text ``message`` / ``suggested_next_action``; it is never
    serialized as a ``next_actions`` record. ``next_actions`` carry ONLY
    read-only MCP inspection (``orcho_run_status`` and ``orcho_run_evidence``
    ``slice='errors'``) — never an external/CLI tool, never a resume of this
    same run.

    This payload is carried by :class:`InspectOnlyControlError`, which both
    ``resume_run`` and the phase-handoff decide guard raise so the two refusals
    stay byte-identical and the mutation tools' success ``outputSchema`` is
    never widened.
    """
    verb = (
        "resume" if attempted == "resume"
        else "record a phase-handoff decision for"
    )
    message = (
        f"Run {run_id} was not started by this MCP server (no durable "
        f"mcp_supervisor.json with a resolvable project_dir), so MCP cannot "
        f"{verb} it. Manage the run through the CLI that started it; from MCP "
        "you can only inspect it."
    )
    suggested_next_action = (
        f"manage run {run_id} via the CLI that started it; from MCP, call "
        "orcho_run_status or orcho_run_evidence(slice='errors') to inspect it"
    )
    next_actions = [
        NextActionRecord(
            intent="Inspect the run's current status and outcome.",
            tool="orcho_run_status",
            args={"run_id": run_id},
            optional=True,
            kind="ready_call",
        ),
        NextActionRecord(
            intent=(
                "Review the run's errors / halt evidence to understand its "
                "state."
            ),
            tool="orcho_run_evidence",
            args={"run_id": run_id, "slice": "errors"},
            optional=True,
            kind="ready_call",
        ),
    ]
    return InspectOnlyControlResult(
        run_id=run_id,
        attempted=attempted,
        reason=reason,
        message=message,
        suggested_next_action=suggested_next_action,
        next_actions=next_actions,
    )


async def start_run(
    task: str | None = None,
    task_file: str | None = None,
    project_dir: str = ".",
    profile: str = "feature",
    mock: bool = False,
    max_rounds: int | None = None,
    mock_validate_plan_reject: int = 0,
    output_mode: Literal["summary", "live", "debug"] = "summary",
    session_mode: Literal["auto", "stateless", "chain", "hybrid"] = "auto",
    attach: list[str] | None = None,
    attach_text: list[str] | None = None,
    attach_image: list[str] | None = None,
    attach_binary: list[str] | None = None,
    from_run_plan: str | None = None,
) -> RunStartedResult:
    """Spawn an orcho pipeline run in the background; return run_id immediately.

    See the ``orcho_run_start`` MCP tool docstring (in ``orcho_mcp.tools``)
    for the wire contract. This module owns the implementation; the
    tool is a thin async shim.

    ``profile`` accepts the ``auto-detect`` selector token alongside the
    executable semantic profiles; the selector threads to the subprocess
    via argv only (never ``ORCHO_PIPELINE``) and core resolves it into a
    concrete profile, surfaced typed in ``orcho_run_status.auto_detect``.
    The default stays ``"feature"``.
    """
    from orcho_mcp.supervisor import get_supervisor

    if (task is None) == (task_file is None):
        raise InvalidPlanError(
            "provide exactly one of 'task' or 'task_file' to orcho_run_start"
        )

    supervisor = get_supervisor()
    with map_command_errors():
        handle = await supervisor.spawn(
            task=task,
            task_file=task_file,
            project_dir=project_dir,
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
    return RunStartedResult(
        run_id=handle.run_id,
        run_dir=str(handle.run_dir),
        pid=handle.pid,
        started_at=handle.started_at,
        project_dir=handle.project_dir,
        command=handle.command,
        next_actions=[
            NextActionRecord(
                intent=(
                    "Enter the watch loop to follow the run until the next "
                    "meaningful change (next event, phase change, handoff, or "
                    "terminal)."
                ),
                tool="orcho_run_watch",
                args={"run_id": handle.run_id},
                optional=True,
                kind="ready_call",
            ),
        ],
    )


def _pending_handoff_or_none(run_id: str) -> PendingHandoffProjection | None:
    """Project the pending-handoff state, swallowing resolution errors.

    Returns ``None`` when the run is not resolvable on disk (a not-yet-
    persisted run, missing workspace, or any read error) so the resume
    path falls through to the supervisor, which keeps its own resolution
    and error contract. Only a positively-read pending handoff triggers
    the structured interception.
    """
    try:
        return project_pending_handoff(run_id)
    except OrchoMCPError:
        return None


def _pending_decision_response(
    run_id: str, pending: PendingHandoffProjection,
) -> ResumePendingDecisionResult:
    """Build the structured pending-decision response for a paused resume.

    The single ``orcho_phase_handoff_decide`` follow-up is typed as
    ``operator_input_required``: the final ``action`` (and any
    ``feedback``) are intentionally *not* substituted, so the record is
    explicitly not a forwardable ``ready_call``. The available decision
    verbs ride in ``choices`` so the operator sees the valid set, and
    ``optional=False`` keeps it the one deterministic next step.
    """
    handoff_id = pending.handoff_id or ""
    available_actions = list(pending.available_actions)
    issue = (
        f"Run {run_id} is paused on a phase handoff "
        f"({pending.phase or 'unknown phase'}) and has no recorded "
        "decision yet, so it cannot resume. Resolve the handoff with "
        "orcho_phase_handoff_decide first."
    )
    return ResumePendingDecisionResult(
        run_id=run_id,
        handoff_id=handoff_id,
        phase=pending.phase,
        status=pending.status or "awaiting_phase_handoff",
        available_actions=available_actions,
        decision_artifact_exists=False,
        issue=issue,
        suggested_next_action=(
            pending.suggested_next_action
            or "call orcho_phase_handoff_decide, then orcho_run_resume"
        ),
        next_actions=[
            NextActionRecord(
                intent=(
                    "Resolve the paused phase handoff before resuming "
                    "(choose an action and, for retry_feedback / "
                    "continue_with_waiver, supply feedback)."
                ),
                tool="orcho_phase_handoff_decide",
                args={"run_id": run_id, "handoff_id": handoff_id},
                optional=False,
                kind="operator_input_required",
                requires_operator_input=True,
                choices=available_actions,
            ),
        ],
    )


def _run_diagnosis_or_none(run_id: str) -> RunDiagnosisProjection | None:
    """Classify the run pre-spawn, swallowing resolution errors to ``None``.

    Defensive — mirrors :func:`_pending_handoff_or_none`. An unresolvable or
    corrupt run yields ``None`` so the resume falls through to the
    supervisor, which keeps its own resolution + error contract (a missing
    run still raises ``RunNotFoundError`` from the supervisor, never a new
    failure surface introduced by the pre-flight classifier).
    """
    try:
        return project_run_diagnosis(run_id)
    except OrchoMCPError:
        return None


def _superseded_response(
    run_id: str, diagnosis: RunDiagnosisProjection,
) -> ResumeBlockedResult:
    """Refuse resume of a parent superseded by an active follow-up child.

    The single follow-up is a ``ready_call`` ``orcho_run_resume`` pre-filled
    with the live child's ``run_id`` (every required arg present), so the
    operator resumes the child that is actually continuing the change
    session rather than diverging by resuming this parent.
    """
    child = diagnosis.recommended_run_id
    message = (
        f"Run {run_id} is superseded by an active follow-up child "
        f"{child}: resuming this parent would diverge from the live child. "
        f"Resume {child} instead."
    )
    next_actions: list[NextActionRecord] = []
    if child:
        next_actions.append(
            NextActionRecord(
                intent=(
                    f"Resume the active follow-up child {child} that is "
                    "continuing this run's change session."
                ),
                tool="orcho_run_resume",
                args={"run_id": child},
                optional=False,
                kind="ready_call",
            ),
        )
    return ResumeBlockedResult(
        run_id=run_id,
        resume_outcome="superseded_by_child",
        status=diagnosis.status or "unknown",
        reason=diagnosis.reason,
        message=message,
        recommended_run_id=child,
        suggested_next_action=(
            f"call orcho_run_resume with run_id={child}"
            if child
            else "inspect the run lineage before resuming"
        ),
        next_actions=next_actions,
    )


def _rejected_terminal_response(
    run_id: str, diagnosis: RunDiagnosisProjection,
) -> ResumeBlockedResult:
    """Refuse resume of a terminal run — inspection only, never a resume.

    A terminal run (terminal success or a terminal halt reason) cannot be
    advanced by resume, so the response carries no spawn fields and the
    ``ready_call`` follow-ups point only at read-only inspection
    (``orcho_run_status`` and ``orcho_run_evidence`` ``slice='errors'``).
    """
    message = (
        f"Run {run_id} is terminal ({diagnosis.reason}); resuming is inert "
        "and would not advance it. Inspect its outcome instead of resuming."
    )
    next_actions = [
        NextActionRecord(
            intent="Inspect the terminal run's status and final outcome.",
            tool="orcho_run_status",
            args={"run_id": run_id},
            optional=True,
            kind="ready_call",
        ),
        NextActionRecord(
            intent=(
                "Review the run's errors / halt evidence to understand why "
                "it ended."
            ),
            tool="orcho_run_evidence",
            args={"run_id": run_id, "slice": "errors"},
            optional=True,
            kind="ready_call",
        ),
    ]
    return ResumeBlockedResult(
        run_id=run_id,
        resume_outcome="rejected_terminal",
        status=diagnosis.status or "terminal",
        reason=diagnosis.reason,
        message=message,
        recommended_run_id=None,
        suggested_next_action=(
            "call orcho_run_status or orcho_run_evidence(slice='errors') to "
            "inspect the terminal run; do not resume"
        ),
        next_actions=next_actions,
    )


def _preflight_blocked_response(
    run_id: str, reason: str, status: str | None,
) -> ResumeBlockedResult:
    """Return a no-spawn refusal produced by core continuation preflight."""
    return ResumeBlockedResult(
        run_id=run_id,
        resume_outcome="preflight_blocked",
        status=status or "unknown",
        reason=reason,
        message=f"Run {run_id} cannot be resumed: {reason}",
        recommended_run_id=None,
        suggested_next_action="inspect the run status and continuation evidence",
        next_actions=[
            NextActionRecord(
                intent="Inspect the run's current status.", tool="orcho_run_status",
                args={"run_id": run_id}, optional=True, kind="ready_call",
            ),
        ],
    )


def _recover_via_source_response(
    run_id: str, diagnosis: RunDiagnosisProjection,
) -> ResumeBlockedResult:
    """Refuse resume of a terminal recovery run; point at the source run.

    This run is a terminal / rejected dead-end whose durable lineage resolved
    a *resumable source* run that still owns the retained checkpoint /
    worktree. Resuming this inert run would spawn a no-op against a terminal
    run (the invariant 'terminal resume never spawns'), so the response carries
    no spawn fields and the single ``ready_call`` ``orcho_run_resume`` is
    pre-filled with the source's ``run_id``.
    """
    source = diagnosis.recommended_run_id
    message = (
        f"Run {run_id} is a terminal recovery run ({diagnosis.reason}); "
        "resuming it is inert. Its retained checkpoint lives on the source "
        f"run {source} — resume {source} instead of starting a new "
        "from_run_plan run."
    )
    next_actions: list[NextActionRecord] = []
    if source:
        next_actions.append(
            NextActionRecord(
                intent=(
                    f"Resume the source run {source} that still owns the "
                    "retained checkpoint / worktree."
                ),
                tool="orcho_run_resume",
                args={"run_id": source},
                optional=False,
                kind="ready_call",
            ),
        )
    return ResumeBlockedResult(
        run_id=run_id,
        resume_outcome="recover_via_source_run",
        status=diagnosis.status or "terminal",
        reason=diagnosis.reason,
        message=message,
        recommended_run_id=source,
        suggested_next_action=(
            f"call orcho_run_resume with run_id={source}"
            if source
            else "inspect the run lineage before resuming"
        ),
        next_actions=next_actions,
    )


def _resume_block_or_none(
    run_id: str, diagnosis: RunDiagnosisProjection,
) -> ResumeBlockedResult | ResumePendingDecisionResult | None:
    """Map a pre-flight diagnosis to a non-applied resume response, or ``None``.

    Returns ``None`` for every resumable condition (running-restart,
    ``failed``, ``interrupted``, non-terminal ``halted``, blocked-worktree)
    so the caller proceeds to ``supervisor.resume``. The non-applied
    conditions intercept before spawn:

    - ``needs_decision`` → :func:`_pending_decision_response`, but only while
      no decision artifact exists yet; once a decision is recorded the run
      falls through to the supervisor (unchanged pre-existing behaviour).
    - ``superseded_by_child`` → :func:`_superseded_response`.
    - ``recover_via_source_run`` → :func:`_recover_via_source_response`
      (points at the resumable source; must intercept BEFORE the supervisor or
      a terminal recovery run would spawn a no-op resume).
    - ``resume_inert_terminal`` → :func:`_rejected_terminal_response`.
    """
    condition = diagnosis.condition
    if condition == "needs_decision":
        pending = _pending_handoff_or_none(run_id)
        if (
            pending is not None
            and pending.is_pending_handoff
            and pending.handoff_id
            and not pending.decision_artifact_exists
        ):
            return _pending_decision_response(run_id, pending)
        return None
    if condition == "superseded_by_child":
        return _superseded_response(run_id, diagnosis)
    if condition == "recover_via_source_run":
        return _recover_via_source_response(run_id, diagnosis)
    if condition == "resume_inert_terminal":
        return _rejected_terminal_response(run_id, diagnosis)
    return None


def _persist_runtime_override(
    run_id: str, override: RuntimeOverrideArg,
) -> None:
    """Fix an operator runtime/model override into the run's durable meta.

    Resolves the run directory and delegates to orcho-core's
    ``sdk.run_control.runtime_override.persist_runtime_override`` — the single
    validation + persistence authority. orcho-core validates
    the ``(runtime, model)`` pair against the configured replacement candidates
    for the phase and writes ``meta['runtime_override']`` idempotently; a
    non-candidate pair raises ``RuntimeOverrideError`` (a ``ValueError``
    subclass) and a divergent re-decision raises ``RuntimeOverrideConflict``.
    Both surface through :func:`map_command_errors` as the typed
    ``InvalidPlanError`` so a bad override is a clean bad-request, never a
    silently-spawned wrong-runtime run.
    """
    from orcho_mcp.services.run_lookup import find_run_dir

    run_dir = find_run_dir(run_id)
    with map_command_errors():
        from sdk.run_control.runtime_override import persist_runtime_override

        persist_runtime_override(
            run_dir,
            phase=override.phase,
            runtime=override.runtime,
            model=override.model,
        )


async def resume_run(
    run_id: str,
    profile: str | None = None,
    runtime_override: RuntimeOverrideArg | None = None,
    operator_intent: Literal["followup", "exit"] | None = None,
    operator_comment: str | None = None,
    ctx: Context | None = None,
) -> (
    RunResumeResult
    | ResumeBlockedResult
    | ResumePendingDecisionResult
    | CorrectionFollowupStartedResult
    | CorrectionOperatorInputRequiredResult
    | CorrectionExitResult
    | CorrectionBlockedResult
):
    """Continue an interrupted run via the supervisor's ``--resume`` path.

    See ``orcho_run_resume`` docstring in ``orcho_mcp.tools`` for the
    wire contract.

    Control guard (first step): before any other pre-flight or the supervisor,
    if the shared diagnosis classifies the run as ``control='inspect_only'``
    (it was not started by this MCP server — no durable supervisor metadata),
    resume is refused by *raising* :class:`InspectOnlyControlError` (carrying the
    typed :class:`InspectOnlyControlResult` payload) and the supervisor is never
    touched. Raising — rather than returning the refusal in the success union —
    keeps this tool's success ``outputSchema`` byte-identical to before the
    guard existed. The guard intercepts BEFORE :func:`_resume_block_or_none` so a
    foreign paused run does not get routed into a decide-then-resume loop MCP
    could never apply. A run with no readable diagnosis falls through to the
    supervisor, which keeps the late ``RunNotFoundError`` for genuinely
    unresolvable / corrupt runs.

    Provider-access recovery: when ``runtime_override`` is supplied,
    the operator's per-phase runtime/model replacement is persisted into the
    run's durable ``meta.json`` *before* the supervisor spawns the resume
    subprocess. The resumed pipeline re-reads that record and applies it to the
    named phase — the override is delivered through durable meta, so the spawn
    arguments stay unchanged. Persisting happens only after the pre-flight guard
    clears (a blocked / terminal resume never writes an override), and a
    non-candidate pair raises before any spawn (no silent fallback).

    Pre-flight guard: before asking the supervisor to spawn, the run is
    classified once via the shared :func:`project_run_diagnosis`. Four
    conditions intercept *before* any spawn so a no-op resume is never
    success-shaped:

    - ``needs_decision`` → :class:`ResumePendingDecisionResult`
      (``resume_outcome='pending_decision'``) while no decision artifact
      exists, pointing at ``orcho_phase_handoff_decide``;
    - ``superseded_by_child`` → :class:`ResumeBlockedResult`
      (``resume_outcome='superseded_by_child'``) recommending the live
      child;
    - ``recover_via_source_run`` → :class:`ResumeBlockedResult`
      (``resume_outcome='recover_via_source_run'``) recommending the
      resumable source run instead of this terminal recovery run;
    - ``resume_inert_terminal`` → :class:`ResumeBlockedResult`
      (``resume_outcome='rejected_terminal'``) pointing at read-only
      inspection.

    Every resumable condition (running-restart / ``failed`` /
    ``interrupted`` / non-terminal ``halted`` / blocked-worktree) falls
    through to ``supervisor.resume`` and returns
    :class:`RunResumeResult` (``resume_outcome='applied'``). The classifier
    is defensive: an unresolvable / corrupt run yields no diagnosis and the
    supervisor stays the resolution + error authority (a missing run still
    raises ``RunNotFoundError`` there), so the guard never adds a new
    failure surface.
    """
    # This durable core classification intentionally precedes the MCP
    # inspect-only gate: a CLI-created correction parent is inspect-only, but
    # MCP may still launch a new, controllable sibling child for it.
    from orcho_mcp.services.continuation import (
        preflight_core_continuation,
        resolve_core_continuation,
    )
    from orcho_mcp.supervisor import get_supervisor
    try:
        continuation = resolve_core_continuation(run_id)
    except OrchoMCPError:
        continuation = None

    # Explicit operator input is authoritative regardless of the continuation
    # subject.  In particular, never quietly turn ``followup`` for a
    # checkpoint-resumable run into a same-run ``resume``: core owns that
    # classification and returns the typed blocker when no follow-up is valid.
    if operator_intent == "exit":
        return CorrectionExitResult(
            run_id=run_id,
            message="Continuation declined; parent run was not modified.",
        )
    if operator_intent == "followup":
        if not (operator_comment or "").strip():
            return _correction_input_result(
                run_id,
                "followup requires a non-empty operator_comment",
            )
        preflight = preflight_core_continuation(
            run_id, intent="followup", operator_comment=operator_comment,
        )
        if preflight.resolution.blocker or preflight.resolution.operation != "start_followup":
            return CorrectionBlockedResult(
                run_id=run_id,
                diff_source=(
                    continuation.diff_source
                    if continuation is not None else None
                ),
                reason=(
                    preflight.resolution.blocker
                    or preflight.resolution.operation
                ),
            )
        supervisor = get_supervisor()
        with map_command_errors():
            handle = await supervisor.followup(
                parent_run_id=run_id,
                operator_comment=operator_comment,
            )
        return CorrectionFollowupStartedResult(
            run_id=handle.run_id,
            parent_run_id=run_id,
            run_dir=str(handle.run_dir),
            pid=handle.pid,
            started_at=handle.started_at,
            project_dir=handle.project_dir,
            command=handle.command,
            next_actions=[],
            suggested_next_action=NextActionRecord(
                intent="Watch the correction child.",
                tool="orcho_run_watch",
                args={"run_id": handle.run_id},
                optional=True,
                kind="ready_call",
            ),
        )

    if continuation is not None and continuation.continuation_subject == "retained_change":
        if continuation.blocked:
            return CorrectionBlockedResult(
                run_id=run_id,
                diff_source=continuation.diff_source,
                reason=continuation.reason,
            )
        if operator_intent is None:
            elicited = await _elicit_correction_input(ctx)
            if elicited is None:
                return _correction_input_result(run_id, continuation.reason)
            operator_intent, operator_comment = elicited
        if operator_intent == "exit":
            return CorrectionExitResult(
                run_id=run_id,
                message="Continuation declined; parent run was not modified.",
            )
        if operator_intent != "followup" or not (operator_comment or "").strip():
            return _correction_input_result(
                run_id,
                "followup requires a non-empty operator_comment",
            )
        preflight = preflight_core_continuation(
            run_id, intent="followup", operator_comment=operator_comment,
        )
        if preflight.resolution.blocker or preflight.resolution.operation != "start_followup":
            return CorrectionBlockedResult(
                run_id=run_id,
                diff_source=continuation.diff_source,
                reason=preflight.resolution.blocker or preflight.resolution.operation,
            )
        supervisor = get_supervisor()
        with map_command_errors():
            handle = await supervisor.followup(
                parent_run_id=run_id,
                operator_comment=operator_comment,
            )
        return CorrectionFollowupStartedResult(
            run_id=handle.run_id,
            parent_run_id=run_id,
            run_dir=str(handle.run_dir),
            pid=handle.pid,
            started_at=handle.started_at,
            project_dir=handle.project_dir,
            command=handle.command,
            next_actions=[],
            suggested_next_action=NextActionRecord(
                intent="Watch the correction child.",
                tool="orcho_run_watch",
                args={"run_id": handle.run_id},
                optional=True,
                kind="ready_call",
            ),
        )

    diagnosis = _run_diagnosis_or_none(run_id)
    if diagnosis is not None:
        if diagnosis.control == "inspect_only":
            raise InspectOnlyControlError(
                build_inspect_only_control_result(
                    run_id,
                    "resume",
                    reason=diagnosis.control_reason or diagnosis.reason,
                ),
            )
        preflight = preflight_core_continuation(run_id, intent="resume")
        blocked = _resume_block_or_none(run_id, diagnosis)
        if preflight.resolution.blocker:
            # Preserve the established terminal/superseded projection while
            # ensuring canonical core preflight ran before any launch path.
            if blocked is not None:
                return blocked
            return _preflight_blocked_response(
                run_id, preflight.resolution.blocker, diagnosis.status,
            )
        if blocked is not None:
            return blocked

    # Deliver the operator runtime/model override by fixing it into the run's
    # durable ``meta.json`` before the resume subprocess spawns.
    # The pipeline re-reads + applies it on resume; the supervisor spawn args
    # are untouched. Validation (configured replacement candidate + registered
    # runtime) lives in orcho-core's ``persist_runtime_override`` and raises a
    # bare ``ValueError`` for a non-candidate pair — mapped here to the typed
    # invalid-request boundary so a bad override never spawns a wrong-runtime
    # run.
    if runtime_override is not None:
        _persist_runtime_override(run_id, runtime_override)

    supervisor = get_supervisor()
    try:
        with map_command_errors():
            handle = await supervisor.resume(run_id, profile=profile)
    except PipelineSpawnError as spawn_error:
        # A launch can race a terminal/finalized write. Re-check through core
        # before surfacing the spawn error, but never turn a genuine failure
        # into a business-state refusal.
        try:
            raced = preflight_core_continuation(run_id, intent="resume")
        except OrchoMCPError as read_error:
            # The failed launch can also race deletion/unavailability of the
            # parent.  In that case there is no canonical state to classify;
            # preserve the original typed spawn failure rather than replacing
            # it with an unrelated read error.
            raise spawn_error from read_error
        if raced.resolution.blocker:
            return _preflight_blocked_response(
                run_id, raced.resolution.blocker,
                diagnosis.status if diagnosis is not None else None,
            )
        raise
    return RunResumeResult(
        run_id=handle.run_id,
        run_dir=str(handle.run_dir),
        pid=handle.pid,
        started_at=handle.started_at,
        project_dir=handle.project_dir,
        command=handle.command,
        resume_outcome="applied",
        message=(
            f"Resume applied — run {handle.run_id} is loading its checkpoint "
            "in a fresh subprocess."
        ),
        suggested_next_action=NextActionRecord(
            intent=(
                "Enter the watch loop to follow the resumed run until the "
                "next meaningful change (next event, phase change, handoff, "
                "or terminal)."
            ),
            tool="orcho_run_watch",
            args={"run_id": handle.run_id},
            optional=True,
            kind="ready_call",
        ),
    )


async def cancel_run(run_id: str, mode: str = "graceful") -> CancelResult:
    """Stop a running pipeline (SIGTERM or SIGKILL via supervisor).

    See ``orcho_run_cancel`` docstring in ``orcho_mcp.tools`` for the
    wire contract.

    An invalid ``mode`` makes ``supervisor.cancel`` raise a bare
    ``ValueError``; ``map_command_errors`` translates it to
    ``InvalidPlanError`` so the bad-request never leaks out of the MCP
    boundary as an untyped exception. The successful-cancel statuses
    (``signal_sent(graceful)`` / ``signal_sent(hard)`` / ``already_dead``
    / ``already_done``) are returns, not exceptions, and pass through
    untouched.
    """
    from orcho_mcp.supervisor import get_supervisor

    supervisor = get_supervisor()
    with map_command_errors():
        result = await supervisor.cancel(run_id, mode=mode)
    return CancelResult(run_id=result["run_id"], status=result["status"])


__all__ = ["cancel_run", "resume_run", "start_run"]
