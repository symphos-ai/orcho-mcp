"""orcho_mcp.schemas.run_control — wire models for run lifecycle tools.

Covers ``orcho_run_start`` / ``orcho_run_resume`` (spawn), ``orcho_run_cancel``
(graceful / hard signal delivery), ``orcho_phase_handoff_decide``
(handoff resolution writes a decision artifact and may flip ``meta.status``),
and ``orcho_delivery_decide`` (post-release delivery / correction gate
resolution).

Wire shapes that ride **inside** an observation response (watch hints,
findings summaries) live in ``observe.py``; this module owns only the
direct lifecycle-tool returns.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from orcho_mcp.schemas.shared import (
    ContinuationSubjectLiteral,
    NextActionRecord,
    ProviderPressure,
    RecommendedNextActionLiteral,
    RecoveryLineage,
)


class RuntimeOverrideArg(BaseModel):
    """Operator runtime/model override for ``orcho_run_resume``.

    Delivers the operator's *replace* decision after a terminal provider-access
    failure: switch the named phase to a different configured ``(runtime,
    model)`` pair and resume. The shape mirrors
    ``sdk.run_control.types.RuntimeOverride`` in orcho-core; the resume path
    validates the pair against the configured replacement candidates and
    persists it into the run's durable ``meta.json`` *before* spawning the
    resume subprocess, so a non-candidate pair aborts the resume rather than
    silently falling back.

    The candidate ``(runtime, model)`` pairs are exactly the ones the SDK
    ``next_actions`` *replace* Action carries (``orcho_run_resume`` with
    ``runtime_override``); callers should pass a value taken from that Action
    rather than inventing a pair.
    """

    model_config = {"extra": "forbid"}

    phase: str = Field(
        description="The pipeline phase whose runtime/model is overridden "
                    "(e.g. ``plan`` / ``implement`` / ``review_changes``). "
                    "The override is isolated to this phase.",
    )
    runtime: str = Field(
        description="The replacement runtime name — must be a registered "
                    "runtime and part of a configured replacement candidate "
                    "pair for the phase.",
    )
    model: str = Field(
        description="The replacement model id paired with ``runtime`` in the "
                    "configured candidate set.",
    )


class RunStartedResult(BaseModel):
    """Returned by ``orcho_run_start`` and ``orcho_run_resume``.

    The pipeline subprocess starts in the background; the run_id, run_dir,
    pid, and started_at are returned immediately so the client can begin
    tracking via ``orcho_run_status`` / ``orcho_run_events_tail`` / progress
    notifications without waiting for completion.
    """
    run_id: str
    run_dir: str
    pid: int
    started_at: str
    project_dir: str
    command: list[str] = Field(
        description="Subprocess argv (for diagnostics — clients should not need to inspect)."
    )
    next_actions: list[NextActionRecord] = Field(
        default_factory=list,
        description=(
            "Suggested follow-up tool calls. After a successful "
            "``orcho_run_start`` spawn this carries a single ready "
            "``ready_call`` to ``orcho_run_watch`` (``args`` pre-filled with "
            "``run_id``), so the client can enter the watch loop immediately "
            "and be woken on the next event / phase change / handoff / "
            "terminal. The applied ``orcho_run_resume`` shape "
            "(:class:`RunResumeResult`) carries its ready watch pointer on "
            "``suggested_next_action`` instead and leaves this list empty."
        ),
    )


class RunResumeResult(RunStartedResult):
    """Returned by ``orcho_run_resume`` when the resume is actually applied.

    A subclass of :class:`RunStartedResult` (same spawn handle: ``run_id`` /
    ``run_dir`` / ``pid`` / ``started_at`` / ``project_dir`` / ``command``)
    plus the typed ``resume_outcome='applied'`` discriminator. The
    pre-flight guard reaches this spawn only when the run is genuinely
    resumable (running-restart, ``failed``, ``interrupted``, or a
    non-terminal ``halted``). A terminal or superseded run never spawns and
    returns :class:`ResumeBlockedResult` instead, so a non-applied resume is
    never success-shaped (no ``pid`` on a no-op).
    """

    resume_outcome: Literal["applied"] = Field(
        default="applied",
        description="Typed outcome discriminator — always ``applied`` for "
                    "this shape; the supervisor spawned a fresh "
                    "checkpoint-loading subprocess.",
    )
    message: str = Field(
        description="Human-readable confirmation that the resume spawned a "
                    "new subprocess loading the existing checkpoint.",
    )
    suggested_next_action: NextActionRecord | None = Field(
        default=None,
        description="Optional single follow-up pointer. On an applied resume "
                    "this is a ready ``ready_call`` to ``orcho_run_watch`` "
                    "(``args`` pre-filled with ``run_id``) so the client can "
                    "re-enter the watch loop on the freshly-spawned "
                    "subprocess; ``None`` when no specific follow-up is "
                    "warranted at spawn time.",
    )


class CorrectionFollowupStartedResult(RunStartedResult):
    """A retained-change correction child launched through MCP."""

    resume_outcome: Literal["followup_started"] = "followup_started"
    parent_run_id: str
    suggested_next_action: NextActionRecord


class CorrectionOperatorInputRequiredResult(BaseModel):
    """A correction resume needs an explicit operator decision before spawn."""

    kind: Literal["operator_input_required"] = "operator_input_required"
    resume_outcome: Literal["operator_input_required"] = "operator_input_required"
    run_id: str
    reason: str
    next_actions: list[NextActionRecord]


class CorrectionExitResult(BaseModel):
    """The operator deliberately declined a retained-change follow-up."""

    resume_outcome: Literal["exit"] = "exit"
    run_id: str
    message: str


class CorrectionBlockedResult(BaseModel):
    """A retained-change correction cannot launch because continuity failed."""

    kind: Literal["correction_blocked"] = "correction_blocked"
    resume_outcome: Literal["blocked"] = "blocked"
    run_id: str
    blocked: Literal[True] = True
    diff_source: Literal["worktree", "artifact", "none"] | None = None
    reason: str


class ResumeBlockedResult(BaseModel):
    """Returned by ``orcho_run_resume`` when resume is refused before spawn.

    Carries **no** spawn fields (no ``pid`` / ``run_dir`` / ``command`` /
    ``started_at``) — the supervisor was never asked to resume. Three typed
    outcomes share this shape, distinguished by ``resume_outcome``:

    - ``rejected_terminal`` — the run is terminal (terminal success or a
      terminal halt reason) with no resumable lineage subject; resuming is
      inert. ``next_actions`` point only at read-only inspection
      (``orcho_run_status`` / ``orcho_run_evidence`` ``slice='errors'``),
      never a resume. ``recommended_run_id`` stays ``None`` (a terminal run
      has no resume target).
    - ``superseded_by_child`` — a newer unfinished follow-up child is
      continuing this run's change session; ``recommended_run_id`` is that
      child and the single ``ready_call`` next action resumes the child
      instead of this parent.
    - ``recover_via_source_run`` — this run is a terminal / rejected recovery
      run, but durable lineage points at a *resumable source* run that still
      owns the retained checkpoint / worktree. ``recommended_run_id`` is that
      source and the single ``ready_call`` next action resumes the source
      instead of spawning a no-op resume against this inert run.
    """

    kind: Literal["resume_blocked"] = "resume_blocked"
    run_id: str
    resume_outcome: Literal[
        "rejected_terminal",
        "superseded_by_child",
        "recover_via_source_run",
    ] = Field(
        description="Typed reason the resume was refused before spawning.",
    )
    status: str = Field(
        description="Merged run status at interception time.",
    )
    reason: str = Field(
        description="One-line factual reason from the run diagnosis "
                    "(status / halt_reason / lineage ids), never parsed "
                    "from log prose.",
    )
    message: str = Field(
        description="Operator-facing explanation of why the resume was "
                    "refused and what to do instead.",
    )
    recommended_run_id: str | None = Field(
        default=None,
        description="The run to resume instead of this one: the active "
                    "follow-up child for ``superseded_by_child``, or the "
                    "resumable source run for ``recover_via_source_run``. "
                    "``None`` for ``rejected_terminal`` (a terminal run with "
                    "no resumable lineage subject has no resume target).",
    )
    suggested_next_action: str = Field(
        description="One-line pointer at the recommended next step.",
    )
    next_actions: list[NextActionRecord] = Field(
        default_factory=list,
        description="Typed follow-up calls — every record is a ``ready_call`` "
                    "carrying all required args: resume-the-child for "
                    "``superseded_by_child``, read-only inspection "
                    "(``orcho_run_status`` / ``orcho_run_evidence``) for "
                    "``rejected_terminal``. Never a resume of a terminal run.",
    )


class ResumePendingDecisionResult(BaseModel):
    """Returned by ``orcho_run_resume`` when the run is still paused.

    A run paused on ``awaiting_phase_handoff`` cannot advance until the
    operator records a phase-handoff decision. Calling ``orcho_run_resume``
    before that decision exists used to surface a raw SDK error; this
    structured response is returned instead so the agent gets an
    actionable next step (``orcho_phase_handoff_decide``) rather than an
    opaque traceback.

    The success path of ``orcho_run_resume`` returns
    :class:`RunResumeResult`; the terminal / superseded refusals return
    :class:`ResumeBlockedResult`. This shape is the third arm of the
    union, which is why the tool's return type spans all three.
    """

    kind: Literal["pending_phase_handoff_decision"] = "pending_phase_handoff_decision"
    resume_outcome: Literal["pending_decision"] = Field(
        default="pending_decision",
        description="Typed outcome discriminator — always "
                    "``pending_decision`` for this shape; the run is paused "
                    "awaiting an operator phase-handoff decision.",
    )
    run_id: str
    handoff_id: str = Field(
        description="Active handoff id the operator must decide on, from "
                    "``meta.phase_handoff.id``.",
    )
    phase: str | None = Field(
        default=None,
        description="Phase that issued the pending handoff, when known.",
    )
    status: str = Field(
        default="awaiting_phase_handoff",
        description="Merged run status at interception time — always "
                    "``awaiting_phase_handoff`` for this response.",
    )
    available_actions: list[str] = Field(
        default_factory=list,
        description="Decision verbs the runtime published for this handoff "
                    "(``continue`` / ``retry_feedback`` / ``halt`` / "
                    "``continue_with_waiver``), verbatim.",
    )
    decision_artifact_exists: bool = Field(
        default=False,
        description="Always ``False`` for this response — the resume was "
                    "intercepted precisely because no decision artifact "
                    "exists yet.",
    )
    issue: str = Field(
        description="Human-readable explanation of why the run could not "
                    "resume and what the operator must do first.",
    )
    suggested_next_action: str = Field(
        description="One-line pointer at the decide-then-resume path.",
    )
    next_actions: list[NextActionRecord] = Field(
        default_factory=list,
        description="Suggested follow-up tool calls — a single "
                    "non-optional ``orcho_phase_handoff_decide`` action "
                    "pre-filled with ``run_id`` and ``handoff_id`` (the "
                    "operator chooses ``action`` / ``feedback``).",
    )


class InspectOnlyControlResult(BaseModel):
    """Typed refusal payload carried by ``InspectOnlyControlError``.

    Shared early-block shape for ``orcho_run_resume`` and
    ``orcho_phase_handoff_decide`` when the target run was NOT started by this
    MCP server (no durable ``mcp_supervisor.json`` with a resolvable
    ``project_dir``). MCP has no durable supervisor metadata to respawn or
    advance such a run — a foreign / CLI-started run dir — so it can only be
    INSPECTED from here. The refusal lands BEFORE the supervisor / SDK is
    touched: no subprocess spawns and no decision artifact is written. There are
    deliberately **no** spawn fields (no ``pid`` / ``run_dir`` / ``command`` /
    ``started_at``).

    This model is *raised* (carried by
    :class:`orcho_mcp.errors.InspectOnlyControlError`), not returned in the
    mutation tools' success union, so their success ``outputSchema`` stays
    byte-identical to before the controllability guard existed. The same
    classification is also exposed read-only on ``orcho_run_diagnose``
    (``control`` / ``control_reason``).

    The control path for the run is the CLI that started it. That instruction
    lives ONLY in the free-text ``message`` + ``suggested_next_action`` — it is
    NOT serialized as a ``next_actions`` record (the next-action contract is
    MCP-tool-only). ``next_actions`` therefore carry ONLY read-only MCP
    inspection: a ``ready_call`` to ``orcho_run_status`` and a ``ready_call`` to
    ``orcho_run_evidence`` (``slice='errors'``). They never reference an
    external/CLI tool and never offer a ``orcho_run_resume`` of this same run.
    """

    kind: Literal["inspect_only"] = "inspect_only"
    run_id: str
    control: Literal["inspect_only"] = Field(
        default="inspect_only",
        description="Durable controllability verdict — always ``inspect_only`` "
                    "for this shape: MCP can inspect but not mutate this run.",
    )
    attempted: Literal["resume", "phase_handoff_decide"] = Field(
        description="Which mutation was refused — ``resume`` "
                    "(``orcho_run_resume``) or ``phase_handoff_decide`` "
                    "(``orcho_phase_handoff_decide``).",
    )
    reason: str = Field(
        description="One-line factual reason from the durable controllability "
                    "classification (e.g. that no ``mcp_supervisor.json`` "
                    "exists), never parsed from log prose.",
    )
    message: str = Field(
        description="Operator-facing explanation that MCP cannot control this "
                    "run and that it must be managed via the CLI that started "
                    "it. This free text — not a ``next_actions`` record — "
                    "carries the CLI instruction.",
    )
    suggested_next_action: str = Field(
        description="One-line pointer: manage the run via its originating CLI; "
                    "from MCP, inspect it with ``orcho_run_status`` / "
                    "``orcho_run_evidence``.",
    )
    next_actions: list[NextActionRecord] = Field(
        default_factory=list,
        description="Read-only MCP inspection only — a ``ready_call`` to "
                    "``orcho_run_status`` and a ``ready_call`` to "
                    "``orcho_run_evidence`` (``slice='errors'``). Never an "
                    "external/CLI tool and never a resume of this run.",
    )


class CancelResult(BaseModel):
    """Outcome of ``orcho_run_cancel``.

    Possible status values:
      - ``signal_sent(graceful)`` — SIGTERM delivered; pipeline catches it,
        flushes checkpoint, exits with ``run.interrupted`` event.
      - ``signal_sent(hard)`` — SIGKILL delivered; in-flight LLM HTTP sockets
        drop, checkpoint reflects only fully-completed phases.
      - ``already_dead`` — process exit not initiated by us; already gone.
      - ``already_done`` — process exited cleanly before the cancel reached it.
    """
    run_id: str
    status: str


class PhaseHandoffDecideResult(BaseModel):
    """Outcome of ``orcho_phase_handoff_decide``.

    A phase-handoff decision is a pure state transition — it writes
    the decision artifact under
    ``<run_dir>/phase_handoff_decisions/{safe_handoff_id}.json`` and
    (for ``action="halt"``) flips ``meta.status`` to ``halted``
    synchronously. ``action="continue"``, ``action="retry_feedback"``,
    and ``action="continue_with_waiver"`` do **not** spawn a process;
    the caller follows up with ``orcho_run_resume`` to actually continue
    execution.

    Fields mirror the persisted decision artifact one-to-one so the
    caller can confirm exactly what was recorded.
    """
    run_id: str
    handoff_id: str
    phase: str
    action: str
    feedback: str | None = None
    note: str | None = None
    decided_at: str = Field(
        description="ISO 8601 UTC timestamp when the decision was recorded.",
    )
    next_actions: list[NextActionRecord] = Field(
        default_factory=list,
        description=(
            "Suggested follow-up tool calls. For ``continue`` / "
            "``retry_feedback`` / ``continue_with_waiver`` actions this "
            "carries a single non-optional ``orcho_run_resume`` action — "
            "the decision API only writes the artifact; the run advances "
            "via resume. For ``halt`` the suggestions are derived from "
            "the post-halt run state."
        ),
    )


class HandoffAdviceSafetyRecord(BaseModel):
    """Safety classification of an advisory recommendation.

    Projected verbatim from orcho-core's ``classify_advice_safety`` (via the SDK
    ``request_handoff_advice`` accessor): ``auto_apply_ok`` is True only for a
    non-low-confidence ``retry_feedback`` (the sole auto-appliable advisory
    action); ``needs_confirmation`` flags low confidence; ``blocked_reason`` is
    set for any non-retry recommendation; ``waiver_blocked`` marks a render-only
    waiver when blocking-severity findings exist. This is advisory metadata — the
    ``orcho_handoff_advice`` tool never applies the recommendation.
    """
    auto_apply_ok: bool = False
    needs_confirmation: bool = False
    blocked_reason: str = ""
    waiver_blocked: bool = False


class HandoffAdviceResult(BaseModel):
    """Returned by ``orcho_handoff_advice``.

    A read-only advisory recommendation for a run paused at
    ``status=awaiting_phase_handoff`` on a rejected/incomplete verdict. The
    advisor is a one-shot read-only LLM pass (orcho-core's
    ``request_handoff_advice``): it writes exactly ONE durable artifact — the
    advice record at ``advice_artifact`` — and NEVER records a phase-handoff
    decision, flips ``meta.status``, or auto-applies anything. The tool only
    recommends.

    ``ready_next_action`` is the deterministic, pre-filled follow-up the operator
    MAY forward — it is a suggestion, not an applied decision:

    - ``recommended_action == 'retry_feedback'`` → a ``ready_call`` to the
      EXISTING ``orcho_phase_handoff_decide`` with
      ``args = {run_id, handoff_id, action='retry_feedback', feedback=<advice
      retry_feedback>, note=<provenance_note>}``. The ``note`` carries provenance
      back to ``advice_artifact`` so an applied retry is auditable. This reuses
      the existing decision verb — there is no new runtime verb and the tool does
      not call decide itself.
    - ``continue`` / ``halt`` → a ``ready_call`` mirroring that verb (with the
      provenance ``note``); ``continue_with_waiver`` →
      ``operator_input_required`` because the operator must supply the waiver
      verdict as ``feedback`` (the advisor's retry text is not a waiver).

    ``provenance_note`` / ``advice_artifact`` are empty only when the advisor
    response was unparseable (no durable advice write — handled like the existing
    advisory paths, never auto-applied).
    """
    run_id: str
    handoff_id: str
    phase: str
    recommended_action: str = Field(
        description="One of ``continue`` / ``retry_feedback`` / ``halt`` / "
                    "``continue_with_waiver`` — the advisor's recommended path.",
    )
    confidence: str = Field(
        description="Advisor confidence: ``high`` / ``medium`` / ``low``.",
    )
    rationale: str
    retry_feedback: str = Field(
        description="The corrective feedback a retry round should act on. "
                    "Non-empty only for a ``retry_feedback`` recommendation.",
    )
    risks: list[str] = Field(default_factory=list)
    expected_files: list[str] = Field(default_factory=list)
    operator_note: str = ""
    parse_warnings: list[str] = Field(default_factory=list)
    safety: HandoffAdviceSafetyRecord
    advice_artifact: str = Field(
        default="",
        description="Run-relative path of the durable advice artifact "
                    "(``phase_handoff_advice/<id>.json``). Empty when the "
                    "advisor response was unparseable.",
    )
    provenance_note: str = Field(
        default="",
        description="The ``feedback_source=...; advice_artifact=...`` note an "
                    "applied ``retry_feedback`` decision must carry. Empty when "
                    "the advisor response was unparseable.",
    )
    ready_next_action: NextActionRecord | None = Field(
        default=None,
        description="Deterministic pre-filled follow-up. For "
                    "``retry_feedback`` a ``ready_call`` to "
                    "``orcho_phase_handoff_decide`` carrying the mandatory "
                    "provenance ``note``; for non-retry recommendations it "
                    "reflects the recommendation without auto-applying it.",
    )
    usage: dict[str, Any] = Field(
        default_factory=dict,
        description="Advisor invocation usage (tokens / duration / cost when "
                    "the provider supplied accounting).",
    )


class DeliveryDecideResult(BaseModel):
    """Outcome of ``orcho_delivery_decide``.

    Mirrors orcho-core's ``DeliveryDecisionResult`` field-for-field. A
    successful decision has ``accepted=True`` and applies the requested
    delivery action. A refused decision is still typed data, not a transport
    error: ``accepted=False`` and ``blocker`` names the current guard
    (for example ``no_pending_delivery_gate`` / ``release_blocked`` /
    ``verification_blocked``).

    ``terminal_outcome`` is deliberately narrow: only ``"done"`` or
    ``"halted"``. A correction request is expressed through
    ``status="fix_requested"`` + ``halt_reason="commit_decision_fix"`` and
    an optional ``followup_run_id``.
    """

    run_id: str
    action: str
    accepted: bool
    status: str
    terminal_outcome: Literal["done", "halted"]
    halt_reason: str | None = None
    artifact_paths: list[str] = Field(default_factory=list)
    commit_sha: str | None = None
    blocker: str | None = None
    followup_run_id: str | None = None
    scope_disclosure: list[str] = Field(
        default_factory=list,
        description=(
            "Per-alias sibling files (``[alias]/rel/path``) implicated when "
            "shipping was refused with ``blocker='delivery_scope_violation'``. "
            "Empty for every non-scope outcome."
        ),
    )


class TypedRunResult(BaseModel):
    """Returned by ``orcho_run_project_typed``.

    Compact response from a *foreground* run driven via the orcho-core
    typed silent boundary (``pipeline.project.app.run_project_pipeline``
    with ``presentation=SILENT, no_interactive=True``). Unlike the
    supervisor-backed ``orcho_run_start`` (which returns immediately
    and runs in the background), this tool **blocks** until the run
    completes and returns the result in one call.

    The pilot is intentionally scoped to short, mock-provider runs.
    Long-running real-provider runs continue to use
    ``orcho_run_start`` + ``orcho_run_watch`` / ``orcho_run_status``
    so the MCP client can stream progress and cancel.

    The response stays compact on purpose. Callers that want the persisted
    session summary / metrics / events follow up with the standard read tools
    using the returned ``run_id``:

      * ``orcho_run_status(run_id)`` — summary meta + metrics snapshot
        (``include=["all"]`` restores full persisted meta).
      * ``orcho_run_metrics(run_id)`` — phase / cost breakdown.
      * ``orcho_run_events_tail(run_id)`` — full events stream.
    """
    run_id: str
    output_dir: str = Field(
        description=(
            "Absolute path to the run directory on disk. The same "
            "directory backs ``orcho_run_status`` reads for this run."
        ),
    )
    status: str = Field(
        description=(
            "Final pipeline status: ``done`` / ``failed`` / "
            "``awaiting_phase_handoff`` / ``halted`` — pulled "
            "directly from the persisted session, not parsed from "
            "any transcript."
        ),
    )
    halt_reason: str | None = Field(
        default=None,
        description=(
            "Structured halt reason. Present on every non-``done`` "
            "terminal status; ``None`` when ``status == 'done'``."
        ),
    )
    event_kinds: list[str] = Field(
        default_factory=list,
        description=(
            "Ordered list of event ``kind`` values from "
            "``events.jsonl``. The canonical spine is ``run.start`` "
            "→ (``phase.start`` / ``phase.end``)+ → ``run.end``; "
            "presence under SILENT proves the file + event sinks "
            "stayed wired. Full event payloads are available via "
            "``orcho_run_events_tail``."
        ),
    )


class TypedRunStartedResult(BaseModel):
    """Returned by ``orcho_run_project_typed_async``.

    Compact start-acknowledgement from the **non-blocking** typed
    silent pilot path. The pipeline runs in a background asyncio task
    via ``asyncio.to_thread``; this response surfaces the run handle
    immediately so the MCP client can begin polling.

    The blocking sibling ``orcho_run_project_typed`` returns
    :class:`TypedRunResult` with the final status in one round-trip;
    the async path returns ``status="running"`` here and the caller
    follows up with the standard read tools to observe progress and
    completion:

      * ``orcho_run_status(run_id)`` — summary meta + metrics; flips to
        ``done`` / ``failed`` / ``awaiting_phase_handoff`` when the
        background task settles the file sink.
      * ``orcho_run_events_tail(run_id)`` — incremental events
        (``run.start`` → ``phase.start`` / ``phase.end`` →
        ``run.end``).

    The async path is workspace-aware: it derives ``output_dir``
    under ``<workspace>/runspace/runs/<run_id>/`` so the existing
    read tools resolve the run by ``run_id`` through the same path
    as supervisor-backed runs. The blocking sibling keeps its
    explicit-``output_dir`` shape; both can coexist.
    """
    run_id: str = Field(
        description=(
            "Pipeline session identifier — the directory name under "
            "``<workspace>/runspace/runs/``. Passed to the existing "
            "read tools (``orcho_run_status`` / "
            "``orcho_run_events_tail``) for status polling."
        ),
    )
    output_dir: str = Field(
        description=(
            "Absolute path to the run directory. Equals "
            "``<workspace>/runspace/runs/<run_id>/`` — the same path "
            "the workspace-aware SDK resolves for ``run_id``."
        ),
    )
    status: str = Field(
        description=(
            "Always ``running`` at this point. Final status surfaces "
            "via ``orcho_run_status(run_id)`` after the background "
            "task completes."
        ),
    )
    started_at: str = Field(
        description=(
            "ISO 8601 UTC timestamp when the background task was "
            "scheduled."
        ),
    )


class RunDiagnosis(BaseModel):
    """Returned by ``orcho_run_diagnose`` — a typed, read-only verdict on a
    run's resume situation plus unambiguously typed next steps.

    ``condition`` is the deterministic classification (first-match priority,
    computed by ``services.run_projection.project_run_diagnosis``):

    - ``active`` — the run is executing; watch / poll it.
    - ``needs_decision`` — paused on ``awaiting_phase_handoff``; an operator
      must record a phase-handoff decision before it can resume.
    - ``needs_delivery_decision`` — paused at an Orcho-managed post-release
      delivery / correction gate; inspect the gate via
      ``orcho_delivery_gate`` and choose one of its ready
      ``orcho_delivery_decide`` calls.
    - ``correction_followup_required`` — the release was rejected and a
      correction was already requested (only ``halt`` remains on the gate);
      resuming this run is inert and a repeated ``fix`` is a no-op, so the typed
      next step is the ``orcho_run_resume`` operator-input requirement for a
      retained-change correction (``recommended_next_action='start_followup'``).
    - ``closed_by_followup`` — this run is a rejected-FA / correction parent
      that a successful correction follow-up CLOSED (orcho-core
      finalization stamped ``superseded_by_followup`` and settled it to
      ``done``). It is terminal/settled, never an active correction: its old
      release_blockers are NOT authoritative, resume is inert, and the
      superseding child rides in ``recommended_run_id``.
    - ``recover_via_source_run`` — this run is a terminal / rejected recovery
      run, but durable lineage points at a *resumable source* run; resume the
      source (``recommended_run_id``), NOT a fresh ``from_run_plan`` against
      this inert run.
    - ``resume_inert_terminal`` — terminal (terminal success or a terminal
      halt reason); resuming is inert, so only inspection is offered. The
      typed ``continuation_subject`` / ``recommended_next_action`` still
      distinguish a plan-only continuation, a clean start-followup, and a
      ``stop_unknown`` dead-end.
    - ``superseded_by_child`` — a newer unfinished follow-up child is
      continuing this run; resume the child (``recommended_run_id``).
    - ``blocked_worktree`` — a follow-up blocked because the parent's
      undelivered diff is not replayable here; resume the parent when known.
    - ``provider_pressure`` — a residual ``halted`` / ``failed`` /
      ``interrupted`` stop that core typed as a provider runtime/access failure
      (rate-limit, transient runtime fault, access loss) in its errors/halt
      rollup, rather than a generic code/test/review failure. The typed
      ``provider_pressure`` field carries the core-typed facts and the
      conservative resume/retry (or wait-for-reset) next steps; this is NOT a
      rejected review / failed acceptance / operator halt.
    - ``halted`` / ``failed`` / ``interrupted`` — a resumable non-terminal
      stop; resume this run or inspect its errors.

    The typed ``continuation_subject`` / ``recommended_next_action`` (projected
    from the same ``services.run_lineage`` resolver that backs
    ``orcho_run_status.recovery_recommendation``) let an agent pick the next
    MCP call without reading output logs, and ``recovery_lineage`` carries the
    durable facts behind that choice. ``from_run_plan`` is recommended ONLY for
    a durable plan artifact (``recommended_next_action='plan_artifact_continuation'``),
    never as a generic way to finish a retained diff or checkpoint.

    ``next_actions`` carry the typed call-readiness contract: a
    ``ready_call`` record's ``args`` already hold every required parameter of
    the target tool (safe to forward verbatim), while an
    ``operator_input_required`` record intentionally omits a final decision
    argument and surfaces ``choices`` / ``input_schema`` for the operator to
    fill. Clients MUST branch on the typed ``kind`` field, never on
    ``intent`` prose.
    """

    run_id: str = Field(description="The diagnosed run.")
    condition: Literal[
        "active",
        "needs_decision",
        "needs_delivery_decision",
        "correction_followup_required",
        "closed_by_followup",
        "recover_via_source_run",
        "resume_inert_terminal",
        "superseded_by_child",
        "blocked_worktree",
        "provider_pressure",
        "halted",
        "failed",
        "interrupted",
    ] = Field(
        description="Deterministic, first-match resume-situation "
                    "classification for the run.",
    )
    continuation_subject: ContinuationSubjectLiteral | None = Field(
        default=None,
        description="The typed durable subject this run's continuation should "
                    "act on (``source_run_checkpoint`` / ``active_child_run`` "
                    "/ ``delivery_gate`` / ``plan_artifact`` / ``none`` / "
                    "``unknown``). ``None`` for conditions that carry no "
                    "lineage subject (e.g. ``needs_decision``).",
    )
    recommended_next_action: RecommendedNextActionLiteral | None = Field(
        default=None,
        description="The typed next action a captain should take "
                    "(``resume_source_run`` / ``resume_active_child`` / "
                    "``delivery_decision`` / ``start_followup`` / "
                    "``plan_artifact_continuation`` / ``stop_unknown``). "
                    "``None`` when no lineage recommendation applies.",
    )
    recovery_lineage: RecoveryLineage | None = Field(
        default=None,
        description="Durable recovery-lineage facts behind the continuation "
                    "recommendation (source pointer + resumability, active "
                    "child, plan-subject availability, and — for a "
                    "``stop_unknown`` dead-end — the missing durable facts). "
                    "``None`` when no recovery lineage was projected.",
    )
    reason: str = Field(
        description="One-line factual explanation assembled from persisted "
                    "state (status / halt_reason / lineage ids); never parsed "
                    "from log prose.",
    )
    status: str | None = Field(
        default=None,
        description="Merged run status at diagnosis time, when resolvable.",
    )
    recommended_run_id: str | None = Field(
        default=None,
        description="The run to resume instead of this one — the active "
                    "follow-up child (``superseded_by_child``) or the known "
                    "parent (``blocked_worktree``). ``None`` otherwise.",
    )
    available_actions: list[str] = Field(
        default_factory=list,
        description="For ``needs_decision``, the phase-handoff decision verbs "
                    "the runtime published (``continue`` / ``retry_feedback`` "
                    "/ ``halt`` / ``continue_with_waiver``). Empty otherwise.",
    )
    decision_recorded: bool = Field(
        default=False,
        description="For ``needs_decision`` only: ``True`` when a phase-handoff "
                    "decision artifact already exists for the active handoff. "
                    "The run stays ``awaiting_phase_handoff`` (continue / "
                    "retry_feedback / continue_with_waiver do not flip the "
                    "status), so the next step is ``orcho_run_resume`` to apply "
                    "the recorded decision — NOT a second "
                    "``orcho_phase_handoff_decide``. ``next_actions`` reflects "
                    "this: a ready resume instead of decide verbs. ``False`` "
                    "(default) for every other condition and for a pending "
                    "handoff with no decision yet.",
    )
    next_actions: list[NextActionRecord] = Field(
        default_factory=list,
        description="Typed follow-up tool calls for this condition. Each "
                    "record carries an unambiguous ``kind`` (``ready_call`` "
                    "vs ``operator_input_required``).",
    )
    provider_pressure: ProviderPressure | None = Field(
        default=None,
        description="Set ONLY for ``condition == 'provider_pressure'``: the "
                    "core-typed provider runtime/access failure (failure_kind, "
                    "recoverable, phase, sanitized message, retry/reset when "
                    "core gives them) plus the conservative typed "
                    "``next_actions`` from the shared "
                    "``build_provider_pressure_next_actions`` helper. ``None`` "
                    "for every other condition and for a generic failure with "
                    "no core-typed provider source.",
    )
    control: Literal["mcp_controllable", "inspect_only"] | None = Field(
        default=None,
        description="Durable controllability axis — ORTHOGONAL to "
                    "``condition``. Whether *this* MCP server can mutate "
                    "(resume / decide) the run or can only inspect it: "
                    "``mcp_controllable`` when the run carries durable "
                    "``mcp_supervisor.json`` state with a resolvable project_dir "
                    "(it was started by this MCP server); ``inspect_only`` when "
                    "the run was NOT started by this MCP server and has no "
                    "durable supervisor metadata, so MCP can only inspect it "
                    "(a foreign / CLI-started run dir). A run can be ``active`` "
                    "/ ``needs_decision`` / terminal AND ``inspect_only`` at the "
                    "same time — the two axes never collapse into one another. "
                    "``None`` when the controllability classification could not "
                    "be read (never defaulted to ``mcp_controllable``).",
    )
    control_reason: str | None = Field(
        default=None,
        description="One-line factual reason behind ``control`` (e.g. the "
                    "resolved durable project_dir, or that no "
                    "``mcp_supervisor.json`` exists). ``None`` when ``control`` "
                    "is ``None``.",
    )


__all__ = [
    "CancelResult",
    "DeliveryDecideResult",
    "HandoffAdviceResult",
    "HandoffAdviceSafetyRecord",
    "InspectOnlyControlResult",
    "PhaseHandoffDecideResult",
    "ResumeBlockedResult",
    "ResumePendingDecisionResult",
    "RunDiagnosis",
    "RunResumeResult",
    "RunStartedResult",
    "TypedRunResult",
    "TypedRunStartedResult",
]
