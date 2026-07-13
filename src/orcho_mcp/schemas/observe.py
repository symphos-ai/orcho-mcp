"""orcho_mcp.schemas.observe — wire models for the observe tool family.

Covers ``orcho_run_events_summary`` (bounded summary used by polling
callers) and ``orcho_run_watch`` (long-poll watch with optional
handoff hints for paused runs).

The handoff models (``HandoffFindingSummary`` / ``HandoffClientHints``
/ ``HandoffDecisionHint``) live here rather than in ``run_control``
because they ride **inside** the watch response — they are observation
payload, not lifecycle action. Run-lifecycle wire shapes (start /
resume / cancel / decide) live in ``run_control.py``.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from orcho_mcp.schemas.shared import ProviderPressure

# ── orcho_run_events_summary ────────────────────────────────────────────────


class CompactRunEvent(BaseModel):
    """Bounded mirror of ``EventRecord``.

    Never carries the full raw payload. The handler extracts a small set of
    well-known payload keys (``summary``/``text``/``message``, ``tool``,
    ``status``) and applies hard character-length caps so a single event
    with a 10 KB ``command`` field cannot inflate the wire payload of a
    200-event summary past the bounded-observe budget. Truncation uses
    Python slicing (Unicode-safe at code-point boundaries); the limits
    are in characters, not bytes.
    """

    seq: int
    ts: str
    kind: str = Field(
        description="Event kind. Truncated to 64 chars to keep the bounded "
                    "summary honest under future emitter changes.",
    )
    phase: str | None = Field(
        default=None,
        description="Phase tag if present. Truncated to 64 chars (same "
                    "reasoning as ``kind``).",
    )
    summary: str | None = Field(
        default=None,
        description="Human-readable summary pulled from one of the canonical "
                    "payload keys (summary / text / message) and truncated "
                    "to 256 chars.",
    )
    tool: str | None = Field(
        default=None,
        description="Tool name when the event reports an agent tool call. "
                    "Truncated to 64 chars.",
    )
    status: str | None = Field(
        default=None,
        description="Status value when the event reports a phase/run state "
                    "transition. Truncated to 64 chars.",
    )


class CurrentSubtaskRecord(BaseModel):
    """Live progress coordinate for the in-flight ``subtask_dag`` subtask.

    Lets a watcher render step-by-step progress ("subtask 3/12 (Patch
    target)") during a long implement phase without scanning raw events.
    Computed from the latest ``subtask.start`` / ``subtask.end`` event up to
    the summary horizon; cleared when the implement phase ends. ``None`` on
    the summary means no subtask is currently in flight.
    """

    subtask_id: str
    index: int = Field(
        description="1-based position of this subtask in the whole DAG.",
    )
    total: int = Field(
        description="Total subtask count in the DAG (the denominator).",
    )
    goal: str = Field(
        default="",
        description="One-line subtask goal, bounded.",
    )
    state: str = Field(
        description='"running" right after start; "done" / "incomplete" / '
                    '"failed" once the subtask ends (incomplete = '
                    "done-criteria attestation gate did not close).",
    )
    seq: int = Field(
        description="Event seq of the start/end boundary that produced this "
                    "snapshot. Advances on every subtask transition — the "
                    "``until=\"subtask\"`` watch fires when it passes the "
                    "caller's cursor.",
    )


class PhaseEventSummary(BaseModel):
    """Per-phase aggregate over the windowed event slice."""

    phase: str = Field(
        description="Phase name, or ``\"(unknown)\"`` for events emitted "
                    "without a phase tag.",
    )
    count: int
    kinds: list[str] = Field(
        description="Distinct event kinds observed in this phase, sorted.",
    )


class PendingHandoffSummary(BaseModel):
    """Active phase-handoff state surfaced inside a run events summary.

    Populated only when the merged run status is
    ``awaiting_phase_handoff`` so polling callers see the pause —
    handoff id, phase, trigger, machine verdict, a coherent round label,
    the available decision actions, a bounded preview of the reviewer's
    last output, whether a decision artifact already exists, and a
    one-line ``suggested_next_action`` — without having to fire an
    ``orcho_run_watch`` handoff trigger. The full structured decision
    packet (findings, choices, elicitation) still rides in
    ``orcho_run_watch``'s ``HandoffDecisionHint``; this is the compact
    at-a-glance projection for status polling.
    """

    handoff_id: str | None = Field(
        default=None,
        description="Handoff identifier from ``meta.phase_handoff.id``.",
    )
    phase: str | None = Field(
        default=None,
        description="Phase that issued the handoff (falls back to the "
                    "run's current phase when the payload omits it).",
    )
    trigger: str | None = Field(
        default=None,
        description="Why the handoff fired (e.g. ``rejected`` / "
                    "``approved`` / ``incomplete``).",
    )
    verdict: str | None = Field(
        default=None,
        description="Machine verdict label carried verbatim from the "
                    "payload (``REJECTED`` / ``APPROVED``).",
    )
    round_label: str | None = Field(
        default=None,
        description="Coherent operator round label — ``<phase> automatic "
                    "round R/M`` for an auto round, ``<phase> human retry "
                    "K`` for a human-directed retry round. Never an "
                    "impossible ``R/M`` with ``R > M``.",
    )
    available_actions: list[str] = Field(
        default_factory=list,
        description="Decision verbs the runtime published for this "
                    "handoff, verbatim.",
    )
    last_output_preview: str | None = Field(
        default=None,
        description="Bounded preview of the reviewer's last critique / "
                    "output. Truncated; never the full raw text.",
    )
    decision_artifact_exists: bool = Field(
        default=False,
        description="True when a phase-handoff decision artifact already "
                    "exists for this handoff (the operator has decided and "
                    "only ``orcho_run_resume`` remains).",
    )
    suggested_next_action: str | None = Field(
        default=None,
        description="One-line pointer at the right next tool: decide then "
                    "resume when no decision exists yet, resume when one "
                    "already does.",
    )


class ProviderSessionFallback(BaseModel):
    """A recovered missing-provider-session fallback, surfaced in a summary.

    orcho-core emits ``phase.provider_session_fallback`` ONLY after a
    fresh-session retry already succeeded — the stale-session error was
    caught and fully handled, no ``failed`` record leaks out, and the run
    continues in the same worktree with persisted context. This record
    makes that recovery observable as structured data and, via
    ``phase_succeeded=True``, makes explicit that the fallback is a
    recovery notice — never a phase failure.
    """

    phase: str | None = Field(
        default=None,
        description="Phase whose provider session was missing and recovered.",
    )
    stale_session_id: str | None = Field(
        default=None,
        description="Redacted preview of the missing provider session id (a "
                    "short prefix + ``…``); never the full identifier. The "
                    "sentinel ``unknown`` passes through when core had no id.",
    )
    fallback_mode: Literal["fresh_provider_session"] = Field(
        default="fresh_provider_session",
        description="Recovery mode — a fresh provider session was started in "
                    "place of the missing one.",
    )
    worktree_preserved: bool = Field(
        default=True,
        description="True — the fallback continues in the same run worktree "
                    "with persisted context (no checkout was lost).",
    )
    phase_succeeded: bool = Field(
        default=True,
        description="True for a recovered fallback — the event is emitted "
                    "only after the fresh-session retry succeeded, so this "
                    "is NOT a phase failure.",
    )


class RetryState(BaseModel):
    """Human-retry / repeated-reject lifecycle state, alongside the pause.

    Derived from raw persisted inputs (round counters, verdict, and the
    ``retry_feedback`` decision artifacts) — never a re-rendered CLI
    banner. Distinguishes an automatic reject, a human-directed retry in
    progress, a human retry that was rejected again, and a retry that was
    accepted (handoff closed).
    """

    retry_context: Literal[
        "automatic_reject",
        "human_retry_in_progress",
        "retry_rejected_again",
        "retry_accepted_closed",
    ] = Field(
        description="Reject / retry lifecycle state. ``automatic_reject`` — "
                    "paused on a rejected verdict at an automatic round; "
                    "``human_retry_in_progress`` — a retry_feedback decision "
                    "exists and the run is executing the human-directed "
                    "round; ``retry_rejected_again`` — paused again on a "
                    "rejected verdict at a human-directed round; "
                    "``retry_accepted_closed`` — the retry was accepted and "
                    "the handoff closed. The two non-paused states are "
                    "best-effort lifecycle hints.",
    )
    retry_attempt_label: str | None = Field(
        default=None,
        description="Coherent operator label for the attempt — "
                    "``<phase> automatic round R/M``, ``<phase> human retry "
                    "K after REJECTED verdict``, or ``<phase> human retry K "
                    "rejected; operator decision required``. Never an "
                    "impossible ``R/M`` with ``R > M``.",
    )
    operator_feedback_preview: str | None = Field(
        default=None,
        description="Bounded preview of the operator's most recent "
                    "retry_feedback text; truncated, never the full text. "
                    "``None`` when no retry feedback was recorded.",
    )
    pending_operator_decision: bool = Field(
        default=False,
        description="True while the run is paused awaiting an operator "
                    "decision on the active handoff (no decision artifact "
                    "recorded for it yet).",
    )


class RunEventsSummary(BaseModel):
    """Bounded summary of a run's recent events.

    Designed for status polling: an operator-side LLM can read this once
    per check instead of dumping the raw event list. ``status`` and
    ``current_phase`` come from the authoritative meta/supervisor merge so
    callers see the same answers ``orcho_run_status`` would give. The
    rest of the aggregate is computed over the ``(since_seq, limit)``
    window. ``next_actions`` are short imperative strings derived from
    ``status`` only — conservative, no invention.

    This is also the fallback half of the resilient observation loop:
    ``orcho_run_watch`` → ``orcho_run_events_summary`` → ``orcho_run_watch``.
    When a bounded watch times out or the client transport drops the
    long-poll, poll this summary with ``since_seq=next_seq`` and then
    resume watching from the returned ``next_seq``. A client-side watch
    disconnect is observer loss, not run failure — the run keeps
    executing in its worktree, and run decisions are taken only from the
    typed ``status`` / ``pending_handoff`` / terminal / evidence signals,
    never from the fact that a watch call ended.
    """

    run_id: str
    total_count: int = Field(
        description="Number of events in the considered window (after "
                    "``since_seq`` and ``limit``).",
    )
    next_seq: int = Field(
        description="Reconnect cursor: the seq to pass as the next "
                    "``since_seq``. Carry it back into ``orcho_run_watch`` "
                    "(or another ``orcho_run_events_summary``) to resume "
                    "observation exactly where this batch ended after a "
                    "bounded watch times out or the client transport drops "
                    "the long-poll.",
    )
    eof: bool = Field(
        description="True when every event past ``since_seq`` fit in this "
                    "batch.",
    )
    status: str | None = Field(
        default=None,
        description="Merged run status (meta.json + supervisor terminal "
                    "fallback). Same value ``orcho_run_status`` returns.",
    )
    current_phase: str | None = Field(
        default=None,
        description="Latest ``phase.start`` seen anywhere up to "
                    "``next_seq`` and not yet closed by a ``phase.end``. "
                    "Computed over the full event stream — not just the "
                    "windowed slice — so polling stays accurate.",
    )
    current_subtask: CurrentSubtaskRecord | None = Field(
        default=None,
        description="Live progress coordinate for the in-flight subtask_dag "
                    "subtask (index/total/goal/state), or ``None`` when no "
                    "subtask is currently running. Computed over the full "
                    "event stream up to ``next_seq``; cleared when the "
                    "implement phase ends.",
    )
    by_phase: list[PhaseEventSummary] = Field(
        default_factory=list,
        description="Per-phase counts and kind vocabulary, in first-seen "
                    "phase order.",
    )
    by_kind: dict[str, int] = Field(
        default_factory=dict,
        description="Total event count per kind across the window.",
    )
    last_n: list[CompactRunEvent] = Field(
        default_factory=list,
        description="The most recent ``last_n`` events from the window, in "
                    "seq order. Each entry is a ``CompactRunEvent`` — no "
                    "raw payload spill.",
    )
    next_actions: list[str] = Field(
        default_factory=list,
        description="Conservative operator guidance derived from "
                    "``status`` only. Empty when status is unknown.",
    )
    pending_handoff: PendingHandoffSummary | None = Field(
        default=None,
        description="Active phase-handoff state when the run is paused on "
                    "``awaiting_phase_handoff``; ``None`` otherwise. Lets a "
                    "polling caller see the pause and its decision surface "
                    "without firing an ``orcho_run_watch`` handoff trigger.",
    )
    provider_session_fallbacks: list[ProviderSessionFallback] = Field(
        default_factory=list,
        description="Recovered missing-provider-session fallbacks observed up "
                    "to ``next_seq`` (phase / redacted stale session id / "
                    "fallback mode / worktree preserved / phase succeeded). "
                    "Each entry is a recovery notice, not a phase failure — "
                    "``phase_succeeded`` is True. Empty when none occurred.",
    )
    retry_state: RetryState | None = Field(
        default=None,
        description="Human-retry / repeated-reject lifecycle state when the "
                    "run is in a reject / retry lifecycle (alongside "
                    "``pending_handoff``); ``None`` otherwise. Lets a client "
                    "tell an automatic reject from a human retry, a repeated "
                    "reject, and an accepted-and-closed retry.",
    )
    provider_pressure: ProviderPressure | None = Field(
        default=None,
        description="Core-typed provider runtime/access failure for a terminal "
                    "run, projected from the same ``project_provider_pressure`` "
                    "source and shared ``build_provider_pressure_next_actions`` "
                    "helper as ``orcho_run_status`` / ``orcho_run_diagnose`` / "
                    "``orcho_run_evidence`` — so every surface agrees on the "
                    "condition and its conservative typed next_actions. ``None`` "
                    "for a non-terminal run or a generic failure with no "
                    "core-typed provider source. Additive: the legacy "
                    "``next_actions: list[str]`` field is unchanged.",
    )


# ── orcho_run_watch ────────────────────────────────────────────────────────


class WatchTrigger(BaseModel):
    """Why ``orcho_run_watch`` returned.

    A single record so callers (and prompts) can branch on ``kind`` without
    parsing free text. ``seq`` is the run's latest event sequence at return
    time and doubles as the reconnect cursor when the caller passed
    ``summary=False``.
    """

    kind: Literal[
        "next_event",
        "phase_change",
        "subtask",
        "handoff",
        "terminal",
        "timeout",
    ]
    reason: str = Field(
        description="Short human-readable reason. Bounded; never quotes raw "
                    "event payload text.",
    )
    seq: int = Field(
        description="Run's ``next_seq`` at return time — the reconnect "
                    "cursor. Use this as the next ``since_seq`` for reconnect "
                    "when ``summary=False``; when ``summary=True`` it carries "
                    "the same value as ``summary.next_seq``. On a "
                    "``timeout`` trigger, or when the client transport drops "
                    "the long-poll, continue observing via "
                    "``orcho_run_events_summary(since_seq=seq)`` and then "
                    "resume ``orcho_run_watch`` from the summary's "
                    "``next_seq`` — a disconnected watch loses the observer, "
                    "not the run.",
    )
    status: str | None = None
    phase: str | None = None


class HandoffFindingSummary(BaseModel):
    """One compact reviewer finding attached to a paused handoff.

    Mirrors a tiny subset of ``FindingRecord`` (severity / title / body /
    required_fix / file / line) with hard length caps so a handoff hint
    can carry up to 5 of these without inflating the wire payload past
    the bounded-observe budget. Raw evidence detail is intentionally
    omitted — the agent renders these to the user as numbered choices,
    not as a debug dump. For full forensic detail, use
    ``orcho_run_evidence(slice="findings")``.
    """

    id: str | None = None
    severity: str | None = Field(
        default=None,
        description="``P1`` / ``P2`` / etc. when the runtime carries one; "
                    "passed through verbatim.",
    )
    title: str = Field(
        description="One-line finding label. Capped at 160 chars.",
    )
    body: str | None = Field(
        default=None,
        description="Short prose explanation when available. Capped at 300 "
                    "chars.",
    )
    required_fix: str | None = Field(
        default=None,
        description="Suggested remediation when the runtime carries one. "
                    "Capped at 240 chars.",
    )
    file: str | None = Field(
        default=None,
        description="Path of the file the finding targets, if any. Capped "
                    "at 240 chars.",
    )
    line: int | None = None


class HandoffClientHints(BaseModel):
    """Hints for the operator-side LLM on how to render the handoff.

    Keyed by ``client`` — one of the first-class profiles ``generic`` /
    ``claude-code`` / ``codex``. Unknown ``interaction_client`` values on
    the watch call normalise to ``generic`` (in
    ``orcho_mcp.client_interactions.get_interaction_profile``), so this
    field always carries a known label.

    This is **presentation metadata only**. None of these flags affect
    run lifecycle, available actions, decision correctness, or the
    behaviour of ``orcho_phase_handoff_decide``. They tell the agent
    *how* to render the prompt, not *what* to do.
    """

    client: str = Field(
        default="generic",
        description="Profile this hint set was rendered for. First-class "
                    "values: ``generic``, ``claude-code``, ``codex``. "
                    "Unknown ``interaction_client`` inputs to "
                    "``orcho_run_watch`` are normalised to ``generic`` "
                    "before this field is populated.",
    )
    interaction_style: Literal["structured_choice", "free_form", "ask"] = "free_form"
    preferred_render: Literal["chat", "ask"] = "chat"
    show_actions: bool = Field(
        default=True,
        description="Whether the agent should render the action list "
                    "explicitly to the user.",
    )
    allow_feedback_text: bool = Field(
        default=True,
        description="Whether the agent should accept free-form feedback "
                    "from the user (relevant when ``retry_feedback`` or "
                    "``continue_with_waiver`` is in ``available_actions``).",
    )
    clarify_on_ambiguous_reply: bool = True
    include_followup_tools: bool = Field(
        default=True,
        description="Whether the agent should also mention the matching "
                    "follow-up tools (``decision_tool`` / ``resume_tool``) "
                    "in the rendered prompt.",
    )
    action_field: str = Field(
        default="action",
        description="Name of the structured ``action`` field the agent "
                    "should pass to ``orcho_phase_handoff_decide``.",
    )
    feedback_field: str = Field(
        default="feedback",
        description="Name of the free-form ``feedback`` field the agent "
                    "should fill when the chosen action requires it.",
    )


class HandoffFollowupCall(BaseModel):
    """Pre-filled follow-up tool call attached to a decision choice.

    Today the only follow-up is ``orcho_run_resume`` after a non-halt
    decision. ``args`` is **always complete** for the followup — there
    is no feedback in resume, ever; the agent can forward it verbatim
    without merging anything in.
    """

    tool: str = Field(
        description="Name of the follow-up MCP tool to call after the "
                    "decision is recorded (today: ``orcho_run_resume``).",
    )
    args: dict[str, Any] = Field(
        description="Complete, ready-to-send arguments for the follow-up "
                    "tool. Safe to forward verbatim — no placeholders.",
    )


class HandoffElicitationHint(BaseModel):
    """Native MCP elicitation metadata for a decision choice.

    This is a progressive-enhancement hint. The choice remains the
    canonical decision contract; clients with form elicitation support
    can let the server request this field natively, while clients
    without elicitation support ask the user in chat and merge the
    answer into ``choices[].args`` under ``field``.
    """

    mode: Literal["form"] = Field(
        default="form",
        description="Elicitation mode used for this choice.",
    )
    client_capability: str = Field(
        default="elicitation.form",
        description="Client capability required for native elicitation. "
                    "Without it, the agent asks the user in chat.",
    )
    field: str = Field(
        description="Name of the field the elicitation response provides "
                    "and the decision tool expects.",
    )
    message: str = Field(
        description="Prompt shown by the MCP client when native "
                    "elicitation is available.",
    )
    requested_schema: dict[str, Any] = Field(
        description="Flat JSON Schema for the native form elicitation "
                    "request.",
    )


class HandoffDecisionChoice(BaseModel):
    """One ready-to-call decision action surfaced inside a handoff hint.

    Pairs a human-readable ``label`` with the structured tool call the
    agent should make: ``tool`` + ``args``. When ``requires_feedback``
    is ``True`` the agent must collect user input first and merge it
    under ``feedback_field`` before calling — ``args`` deliberately
    omits the feedback key so a weak agent cannot forward a placeholder
    string. When ``requires_feedback`` is ``False``, ``args`` is
    complete and safe to send as-is.

    ``followup`` carries the next step after the decision is recorded
    (today: ``orcho_run_resume`` for non-halt actions). ``None`` for
    terminal actions (``halt``).
    """

    label: str = Field(
        description="One-line human-readable description of the choice.",
    )
    action: str = Field(
        description="The decision verb (``continue`` / ``retry_feedback`` "
                    "/ ``continue_with_waiver`` / ``halt``).",
    )
    tool: str = Field(
        description="MCP tool to call for the decision (today: "
                    "``orcho_phase_handoff_decide``).",
    )
    args: dict[str, Any] = Field(
        description="Safely pre-filled tool arguments. NEVER contains a "
                    "placeholder string the agent might forward verbatim. "
                    "When ``requires_feedback=True``, the feedback kwarg "
                    "is omitted — the agent collects user input and "
                    "merges it in under ``feedback_field``.",
    )
    requires_feedback: bool = Field(
        description="``True`` when the action needs a free-form feedback "
                    "string from the user before the call. ``False`` "
                    "means ``args`` is complete and safe to send as-is.",
    )
    feedback_field: str | None = Field(
        default=None,
        description="Name of the kwarg to fill when ``requires_feedback`` "
                    "is True (e.g. ``feedback``). ``None`` otherwise.",
    )
    feedback_placeholder: str | None = Field(
        default=None,
        description="Human-readable hint the agent surfaces to the user "
                    "when collecting feedback. ``None`` when no feedback "
                    "is needed.",
    )
    followup: HandoffFollowupCall | None = Field(
        default=None,
        description="The tool call to make after the decision is recorded. "
                    "Today: ``orcho_run_resume`` with ``{run_id}`` for "
                    "``continue`` / ``retry_feedback`` / "
                    "``continue_with_waiver``; ``None`` for "
                    "``halt`` (terminal — no resume).",
    )
    elicitation: HandoffElicitationHint | None = Field(
        default=None,
        description="Native MCP elicitation metadata for feedback-gated "
                    "choices. Present for ``retry_feedback`` and "
                    "``continue_with_waiver`` so clients with form "
                    "elicitation support can collect feedback natively; "
                    "``None`` for choices that do not require feedback.",
    )


class HandoffDecisionHint(BaseModel):
    """Structured decision packet surfaced when the run pauses for a human.

    Carries compact ``findings``, a ``default_action`` heuristic,
    ``feedback_required_for`` derived from available actions, and
    ``client_hints`` for rendering. The MCP-layer presentation adapter
    behind ``client_hints`` is selected by
    ``orcho_run_watch.interaction_client``. The agent is responsible for
    the actual conversation; Orcho returns the data and the agent renders
    the prompt.

    ``choices`` is the ready-to-call decision menu: one
    ``HandoffDecisionChoice`` per known runtime action with
    pre-filled ``args``, ``requires_feedback`` flag, and ``followup``
    pointer. Agents can branch on ``choices`` without parsing
    ``available_actions`` and without re-deriving the
    ``orcho_phase_handoff_decide`` argument shape.

    Additive: existing clients that only read ``handoff_id`` /
    ``phase`` / ``available_actions`` continue to work. Client-specific
    runtime integrations (IDE-side UI, native pickers, automatic
    decision execution) remain out of scope here — this is presentation
    metadata only.
    """

    kind: Literal["requires_user_decision"] = "requires_user_decision"
    run_id: str = Field(
        description="Run this handoff belongs to. Duplicated from the "
                    "top-level ``RunWatchResult.run_id`` so the handoff "
                    "object is self-contained when copied into a prompt.",
    )
    handoff_id: str | None = Field(
        default=None,
        description="Handoff identifier from ``meta.phase_handoff.id`` when "
                    "available.",
    )
    phase: str | None = Field(
        default=None,
        description="Phase that issued the handoff. Prefers "
                    "``meta.phase_handoff.phase``; falls back to the run's "
                    "current_phase. May be ``None`` when neither is known.",
    )
    title: str = Field(
        description="Short one-line label for prompts/UI. Conservative — "
                    "never invents review findings.",
    )
    findings_summary: str | None = Field(
        default=None,
        description="Bounded human-readable summary built from the compact "
                    "``findings`` list (severity + title only). ``None`` "
                    "when no findings are available. Capped at 500 chars.",
    )
    findings: list[HandoffFindingSummary] = Field(
        default_factory=list,
        description="Up to 5 compact reviewer findings. Source of truth: "
                    "``meta.phase_handoff.findings`` if list-like; "
                    "otherwise a defensive fallback to the SDK findings "
                    "API scoped to the handoff phase. Empty list is a "
                    "valid value — never invented.",
    )
    available_actions: list[str] = Field(
        default_factory=list,
        description="Normalised list of decision actions from "
                    "``meta.phase_handoff.available_actions``. Empty when "
                    "meta does not carry them.",
    )
    default_action: str | None = Field(
        default=None,
        description="Suggested action when the agent has no explicit user "
                    "preference. Chosen from ``available_actions`` only "
                    "(``retry_feedback`` > ``continue`` > ``halt`` > "
                    "``continue_with_waiver``; the waiver override sits "
                    "last and is never suggested unsolicited). "
                    "``None`` if no actions are available — Orcho never "
                    "suggests an action the runtime did not offer.",
    )
    feedback_required_for: list[str] = Field(
        default_factory=list,
        description="Actions that require a free-form ``feedback`` string "
                    "alongside the structured ``action`` field.",
    )
    decision_tool: str = "orcho_phase_handoff_decide"
    resume_tool: str = "orcho_run_resume"
    recommended_user_prompt: str = Field(
        description="Bounded prompt the operator-side LLM can render to "
                    "the user. Includes phase, findings (when available), "
                    "and the structured-choice action list. Capped at 1500 "
                    "chars. No raw event payload text.",
    )
    client_hints: HandoffClientHints = Field(
        default_factory=HandoffClientHints,
        description="Rendering hints keyed by the resolved interaction "
                    "client. Tells the agent which fields to fill, "
                    "whether to clarify ambiguous replies, and the "
                    "preferred render style. Client-specific runtime "
                    "integrations (IDE UI, native pickers, automatic "
                    "decision execution) remain out of scope.",
    )
    choices: list[HandoffDecisionChoice] = Field(
        default_factory=list,
        description="Ready-to-call decision menu. One entry per known "
                    "action in ``available_actions`` (today: "
                    "``continue`` / ``retry_feedback`` / "
                    "``continue_with_waiver`` / ``halt``). "
                    "Each choice carries pre-filled tool args, a "
                    "``requires_feedback`` flag, and a ``followup`` "
                    "pointer to the resume step (when applicable). "
                    "Unknown runtime verbs are filtered out so the "
                    "menu only contains callable actions — "
                    "``available_actions`` keeps the verbatim "
                    "runtime offering for callers that need the raw set.",
    )


class RunWatchResult(BaseModel):
    """Result of one ``orcho_run_watch`` call.

    Reconnect rule: the caller's next ``since_seq`` is
    ``result.summary.next_seq`` when ``summary=True`` and
    ``result.trigger.seq`` otherwise — both fields carry the same value
    at return time, so callers may always use ``trigger.seq`` as a uniform
    fallback. Raw event payloads are never returned.

    The reconnect cursor anchors the resilient observation loop
    ``orcho_run_watch`` → ``orcho_run_events_summary`` → ``orcho_run_watch``:
    on a ``timeout`` trigger, or when the client transport drops the
    long-poll, continue with
    ``orcho_run_events_summary(since_seq=trigger.seq)`` and then resume
    watching from that summary's ``next_seq``. A client-side disconnect of
    the watch is observer loss, not a failed run — the run keeps executing
    and lifecycle decisions are read only from the typed ``status`` /
    ``handoff`` / terminal / evidence signals, never from a watch call
    ending early.
    """

    run_id: str
    triggered: bool = Field(
        description="True when a trigger condition fired before timeout. "
                    "False only on the timeout path (``trigger.kind == "
                    "\"timeout\"``).",
    )
    trigger: WatchTrigger
    summary: RunEventsSummary | None = Field(
        default=None,
        description="Bounded ``RunEventsSummary`` snapshot at return time "
                    "when the caller requested ``summary=True``; otherwise "
                    "``None``.",
    )
    handoff: HandoffDecisionHint | None = Field(
        default=None,
        description="Populated only when ``trigger.kind == \"handoff\"`` "
                    "and the meta carries a usable ``phase_handoff`` block.",
    )


# ── orcho_run_live_status ───────────────────────────────────────────────────


class RunLiveActivity(BaseModel):
    """Compact "what just happened" coordinate for the live status card.

    Projected from the run's most recent event — bounded the same way a
    ``CompactRunEvent`` is, so the high-frequency live-status poll never
    spills raw payload text. ``preview`` is the first non-empty of the
    event's summary / status / tool, truncated; ``None`` when the event
    carried nothing human-readable.
    """

    kind: str = Field(
        description="Kind of the most recent event (e.g. ``phase.start`` / "
                    "``subtask.end`` / ``run.end``).",
    )
    ts: str | None = Field(
        default=None,
        description="Timestamp of the most recent event, verbatim.",
    )
    phase: str | None = Field(
        default=None,
        description="Phase tag of the most recent event, if any.",
    )
    preview: str | None = Field(
        default=None,
        description="Bounded preview of the event's human-readable text "
                    "(summary / status / tool). Truncated; never the full "
                    "raw payload.",
    )


class RunLiveHandoff(BaseModel):
    """Compact pending-handoff slice for the live status card.

    Reuses the existing handoff projections (``project_pending_handoff``
    for the operator fields + ``build_handoff_hint`` for the
    ``default_action`` heuristic and ``findings_summary``) — the
    ``meta.phase_handoff`` payload is parsed only in the projection owner,
    never here. Carries just enough to render the pause at a glance; the
    full structured decision packet still rides in ``orcho_run_watch``'s
    ``HandoffDecisionHint``.
    """

    handoff_id: str | None = Field(
        default=None,
        description="Handoff identifier from the pending-handoff projection.",
    )
    phase: str | None = Field(
        default=None,
        description="Phase that issued the handoff.",
    )
    available_actions: list[str] = Field(
        default_factory=list,
        description="Decision verbs the runtime published, verbatim.",
    )
    default_action: str | None = Field(
        default=None,
        description="Suggested action from the handoff-hint heuristic "
                    "(``retry_feedback`` > ``continue`` > ``halt`` > "
                    "``continue_with_waiver``); ``None`` when none offered.",
    )
    verdict: str | None = Field(
        default=None,
        description="Machine verdict label (``REJECTED`` / ``APPROVED``) "
                    "carried verbatim from the projection.",
    )
    findings_summary: str | None = Field(
        default=None,
        description="Bounded one-line severity+title aggregate of the "
                    "reviewer findings; ``None`` when no findings.",
    )
    recommended_action: str | None = Field(
        default=None,
        description="One-line pointer at the right next tool — decide then "
                    "resume when no decision exists yet, resume when one "
                    "already does.",
    )


class RunLiveTerminal(BaseModel):
    """Terminal-state slice for the live status card.

    Present only for a terminal ``state_class`` (``terminal_success`` /
    ``terminal_halted`` / ``terminal_inconsistent``). Carries the resolved
    ``halt_reason``, the ``final_acceptance`` verdict, whether a resume is
    meaningful, any detected consistency violations, and — for a run whose
    Orcho-managed delivery already landed — the delivery disposition
    (``delivery_committed`` / ``delivery_published`` / ``delivery_pr_url``), so
    a caller can render a coherent terminal card without scraping logs.
    """

    halt_reason: str | None = Field(
        default=None,
        description="Resolved halt reason (meta + supervisor fallback); "
                    "``None`` for a clean terminal success.",
    )
    final_acceptance: str | None = Field(
        default=None,
        description="Normalised final-acceptance verdict "
                    "(``APPROVED`` / ``REJECTED``) from "
                    "``meta.phases.final_acceptance``; ``None`` when the run "
                    "carries no final-acceptance phase.",
    )
    final_acceptance_rejected: bool = Field(
        default=False,
        description="True when the final-acceptance verdict is a rejection — "
                    "the explicit rejection flag a halted terminal card "
                    "surfaces alongside ``halt_reason``.",
    )
    resume_meaningful: bool = Field(
        description="Whether resuming the run can still make progress — "
                    "``True`` for halted / awaiting states, ``False`` for a "
                    "clean terminal success (resume would be inert).",
    )
    inconsistencies: list[str] = Field(
        default_factory=list,
        description="Detected terminal contradictions (e.g. a terminal "
                    "success status while final_acceptance is REJECTED). "
                    "Empty for a coherent terminal card.",
    )
    delivery_committed: bool = Field(
        default=False,
        description="True when the run's Orcho-managed delivery already landed "
                    "in the target checkout (a ``committed`` / "
                    "``applied_uncommitted`` delivery). ``False`` when no "
                    "delivery landed or the run carries no delivery block.",
    )
    delivery_published: bool = Field(
        default=False,
        description="True when the landed delivery opened a pull request "
                    "(``delivery_pr_url`` is present). ``False`` otherwise.",
    )
    delivery_pr_url: str | None = Field(
        default=None,
        description="The live pull-request URL from the run's delivery block "
                    "when the delivery was published; ``None`` when no pull "
                    "request was opened.",
    )


class RunLiveStatusCard(BaseModel):
    """Bounded operator-safe live status of a mono run.

    A single typed snapshot uniting the durable meta status (with
    supervisor terminal fallback), the live phase/subtask position, the
    last significant activity, any pending phase-handoff, and terminal
    consistency — without raw log scraping. Designed for high-frequency
    polling: every embedded preview is truncated, and no full phase
    bodies / critiques / raw logs ride in the payload.

    ``state_class`` is the single classification a caller branches on:

    - ``running_phase`` — executing a phase, no subtask in flight;
    - ``running_subtask`` — executing a ``subtask_dag`` subtask
      (``current_subtask`` carries index/total/goal/state);
    - ``awaiting_handoff`` — paused on a phase-handoff decision
      (``pending_handoff`` populated);
    - ``terminal_success`` — a clean terminal success;
    - ``terminal_halted`` — a halted / failed / interrupted terminal;
    - ``terminal_inconsistent`` — a terminal success whose
      final_acceptance contradicts it (e.g. ``done`` + ``REJECTED``); the
      contradiction is surfaced in ``consistency_flags``, never hidden.
    """

    run_id: str
    status: str | None = Field(
        default=None,
        description="Merged run status (meta.json + supervisor terminal "
                    "fallback). Same value ``orcho_run_status`` returns.",
    )
    state_class: Literal[
        "running_phase",
        "running_subtask",
        "awaiting_handoff",
        "terminal_success",
        "terminal_halted",
        "terminal_inconsistent",
    ] = Field(
        description="Single typed classification of the run's live state.",
    )
    current_phase: str | None = Field(
        default=None,
        description="Latest open phase (``phase.start`` not yet closed by a "
                    "``phase.end``); ``None`` between phases or once "
                    "terminal.",
    )
    current_subtask: CurrentSubtaskRecord | None = Field(
        default=None,
        description="Live progress coordinate for the in-flight "
                    "subtask_dag subtask (index/total/goal/state), or "
                    "``None`` when no subtask is currently running.",
    )
    last_activity: RunLiveActivity | None = Field(
        default=None,
        description="Compact projection of the most recent event "
                    "(kind/ts/phase/bounded preview); ``None`` when the run "
                    "has no events yet.",
    )
    pending_handoff: RunLiveHandoff | None = Field(
        default=None,
        description="Compact pending phase-handoff slice when the run is "
                    "paused on ``awaiting_phase_handoff``; ``None`` "
                    "otherwise.",
    )
    terminal: RunLiveTerminal | None = Field(
        default=None,
        description="Terminal-state slice (halt_reason / final_acceptance / "
                    "resume_meaningful / inconsistencies) for a terminal "
                    "``state_class``; ``None`` while the run is live.",
    )
    next_action: str | None = Field(
        default=None,
        description="One-line conservative next-step pointer derived from "
                    "``state_class`` only — never invented.",
    )
    consistency_flags: list[str] = Field(
        default_factory=list,
        description="Detected status/verdict contradictions surfaced "
                    "explicitly (e.g. terminal success while "
                    "final_acceptance is REJECTED). Empty when coherent.",
    )
    next_seq: int | None = Field(
        default=None,
        description="Reconnect cursor: the latest event seq, carry it into "
                    "``orcho_run_watch`` / ``orcho_run_events_summary`` to "
                    "resume observation from here. ``None`` when the run has "
                    "no events.",
    )
    provider_pressure: ProviderPressure | None = Field(
        default=None,
        description="Core-typed provider runtime/access failure when this "
                    "terminal card represents a provider-pressure stop, "
                    "projected from the same ``project_provider_pressure`` "
                    "source / shared helper as the other surfaces. When set, "
                    "``next_action`` reads as a resume-later/inspect pointer, "
                    "NOT a review/delivery/operator-halt rejection. ``None`` "
                    "otherwise.",
    )


__all__ = [
    "CompactRunEvent",
    "CurrentSubtaskRecord",
    "HandoffClientHints",
    "HandoffDecisionHint",
    "HandoffElicitationHint",
    "HandoffFindingSummary",
    "PendingHandoffSummary",
    "PhaseEventSummary",
    "ProviderSessionFallback",
    "RetryState",
    "RunEventsSummary",
    "RunLiveActivity",
    "RunLiveHandoff",
    "RunLiveStatusCard",
    "RunLiveTerminal",
    "RunWatchResult",
    "WatchTrigger",
]
