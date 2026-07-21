"""orcho_mcp.observe.handoff_hints — paused-run handoff decision hint builder.

Synthesises an enriched ``HandoffDecisionHint`` for a paused run (status
``awaiting_phase_handoff`` / ``awaiting_gate_decision``). The agent
consumes the hint and runs the conversation with the user; Orcho stays
bounded and never reads stdin.

This module is **presentation-only**: the ``meta.phase_handoff``
read-model (normalised actions / id / phase / trigger / incomplete-count
+ the resolved findings source, including the SDK findings fallback) is
projected by :func:`orcho_mcp.services.run_projection.project_handoff_read_model`.
``build_handoff_hint`` builds the prompt, choices, client-hints, default
action, and bounded findings compaction on top of that read-model. The
dependency direction is ``observe → services`` only.

Backed by ``build_handoff_hint`` (public service entry); the matching
``@mcp.tool`` consumers (``orcho_run_watch``) invoke it through this
module. Defensive: malformed meta/findings must never raise — the user
needs the decision prompt at the exact moment the runtime paused, not
a 500.
"""
from __future__ import annotations

from orcho_mcp.client_interactions import (
    get_interaction_profile,
    profile_to_client_hints,
)
from orcho_mcp.observe.summary import _truncate
from orcho_mcp.schemas import (
    HandoffDecisionChoice,
    HandoffDecisionHint,
    HandoffElicitationHint,
    HandoffFindingSummary,
    HandoffFollowupCall,
    RunEventsSummary,
)
from orcho_mcp.services.run_projection import project_handoff_read_model

# The constants and helpers below turn a paused-run payload into a
# structured decision packet the agent can render directly. Orcho stays
# bounded; the agent owns the conversation.

# Severity-ordered action preference for ``default_action``. ``retry_feedback``
# is the safest default after a reject because it loops back through the
# pipeline; ``continue`` skips the gate; ``halt`` is terminal.
# ``continue_with_waiver`` is intentionally last: it is a deliberate
# override that bypasses the gate while recording a durable operator
# waiver, never the safe auto-default Orcho should suggest unsolicited —
# so on a rejected handoff (which always offers continue / retry_feedback
# / halt) it is never picked, but it stays in the tuple so the preference
# list enumerates the full action vocabulary.
_HANDOFF_DEFAULT_ACTION_PREFERENCE: tuple[str, ...] = (
    "retry_feedback", "continue", "halt", "continue_with_waiver",
)

# Actions that require a free-form ``feedback`` string alongside the
# structured ``action`` field. ``retry_feedback`` injects it as the next
# round's critique; ``continue_with_waiver`` records it as the durable
# operator waiver.
_HANDOFF_FEEDBACK_ACTIONS: frozenset[str] = frozenset({
    "retry_feedback", "continue_with_waiver",
})

# Known decision verbs. ``choices[]`` builds entries only for actions
# in this set so unknown runtime verbs (forward-compat) never surface
# as callable menu items — the agent would otherwise blindly forward
# an unknown ``action`` to ``orcho_phase_handoff_decide``.
# ``available_actions`` keeps the verbatim runtime offering for callers
# that need the raw set.
_HANDOFF_KNOWN_ACTIONS: frozenset[str] = frozenset({
    "continue", "retry_feedback", "halt", "continue_with_waiver",
})

# Operator-side prompt the agent surfaces to the user when collecting
# feedback for ``retry_feedback``. Bounded; consumed by the agent, not
# rendered to the user as-is unless the client renders it that way.
_HANDOFF_FEEDBACK_PLACEHOLDER = (
    "Explain what the reviewer should reconsider — be concrete about "
    "the finding, the change you want, or the missing context."
)
_HANDOFF_FEEDBACK_ELICITATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "feedback": {
            "type": "string",
            "title": "Feedback",
            "description": _HANDOFF_FEEDBACK_PLACEHOLDER,
            "minLength": 1,
        },
    },
    "required": ["feedback"],
    "additionalProperties": False,
}

# Decision tool name and follow-up resume tool name. Constants so the
# agent-facing payload matches the names registered in tools.py.
_HANDOFF_DECISION_TOOL = "orcho_phase_handoff_decide"
_HANDOFF_RESUME_TOOL = "orcho_run_resume"

# Bounded caps for the compact finding fields. The handoff hint must stay
# inside the bounded-observe budget even when the meta carries 5 findings
# of pathological length.
_HANDOFF_FINDING_TITLE_MAX = 160
_HANDOFF_FINDING_BODY_MAX = 300
_HANDOFF_FINDING_FIX_MAX = 240
_HANDOFF_FINDING_FILE_MAX = 240
_HANDOFF_FINDINGS_LIMIT = 5
_HANDOFF_FINDINGS_SUMMARY_MAX = 500
_HANDOFF_PROMPT_MAX = 1500

# Static action-line copy. Keeping this in one place so the prompt stays
# stable across builds — clients can reasonably string-match these.
_HANDOFF_ACTION_DESCRIPTIONS: dict[str, str] = {
    "retry_feedback": "give feedback and re-plan",
    "continue": "override and continue",
    "halt": "stop the run",
    "continue_with_waiver": "override with a recorded waiver",
}

# Statuses that signal a paused run requiring a handoff decision. Mirrors
# the set in observe.watch — kept local so handoff_hints stays
# self-contained (watch imports from us, not vice versa).
_HANDOFF_STATUSES: frozenset[str] = frozenset({
    "awaiting_phase_handoff", "awaiting_gate_decision",
})


def _handoff_pause_summary(
    run_id: str,
    phase: str | None,
    trigger: str | None,
    incomplete_count: int,
) -> str:
    """One-line natural-language pause header for the prompt/title.

    When the runtime paused ``implement`` on an ``incomplete`` trigger,
    surface the incomplete-subtask count directly ("paused at implement:
    N subtask(s) incomplete") so the operator sees *why* without
    inspecting artifacts. Every other phase/trigger keeps the generic
    "paused at <phase>" phrasing — the special branch needs BOTH the
    implement phase AND the incomplete trigger.
    """
    if phase == "implement" and trigger == "incomplete":
        noun = "subtask" if incomplete_count == 1 else "subtasks"
        return (
            f"Orcho run {run_id} paused at implement: "
            f"{incomplete_count} {noun} incomplete."
        )
    return f"Orcho run {run_id} paused at {phase or 'unknown'}."


def _handoff_default_action(actions: list[str]) -> str | None:
    """Pick a safe default from ``available_actions``.

    Preference order is ``retry_feedback`` > ``continue`` > ``halt`` >
    ``continue_with_waiver`` — the waiver override sits last because it
    deliberately bypasses the gate, so Orcho never suggests it unsolicited.
    Refuses to suggest an action that is not on the offered list — Orcho
    never invents capabilities the runtime did not advertise.
    """
    for candidate in _HANDOFF_DEFAULT_ACTION_PREFERENCE:
        if candidate in actions:
            return candidate
    return None


def _handoff_feedback_required_for(actions: list[str]) -> list[str]:
    """Subset of ``actions`` that require free-form feedback alongside
    the structured action choice."""
    return [a for a in actions if a in _HANDOFF_FEEDBACK_ACTIONS]


def _build_choices(
    run_id: str, handoff_id: str | None, actions: list[str],
) -> list[HandoffDecisionChoice]:
    """Build the ready-to-call decision menu for a paused run.

    Filters ``actions`` down to the known verbs in
    ``_HANDOFF_KNOWN_ACTIONS`` so unknown runtime verbs never become
    callable menu items — the agent would otherwise blindly forward
    an unknown ``action`` string to ``orcho_phase_handoff_decide``.
    Preserves the runtime's order within the known set (the runtime
    knows its own preference).

    Per-action rules, derived rather than hand-listed so a new verb only
    needs registering in the action sets above:

    - **feedback-gated** (``retry_feedback`` / ``continue_with_waiver``,
      i.e. ``_HANDOFF_FEEDBACK_ACTIONS``): ``requires_feedback=True``,
      ``feedback_field="feedback"`` (the kwarg name on
      ``orcho_phase_handoff_decide``), and ``feedback_placeholder``
      surfacing operator-side prompt copy. ``args`` deliberately
      OMITS the feedback key so a weak agent cannot forward a
      placeholder string. Followup: ``orcho_run_resume`` with
      ``{run_id}``.
    - **``continue``**: ``requires_feedback=False``, ``args`` is
      complete (``run_id``, ``handoff_id``, ``action``), safe to
      send as-is. Same resume followup.
    - **``halt``**: ``requires_feedback=False``, ``args`` complete,
      ``followup=None`` (halt is terminal — no resume).
    """
    resume_followup = HandoffFollowupCall(
        tool=_HANDOFF_RESUME_TOOL, args={"run_id": run_id},
    )
    base_args: dict[str, object] = {"run_id": run_id}
    if handoff_id is not None:
        base_args["handoff_id"] = handoff_id

    out: list[HandoffDecisionChoice] = []
    for action in actions:
        if action not in _HANDOFF_KNOWN_ACTIONS:
            continue
        # ``args`` is per-choice — start from base_args, layer in
        # the action verb. Never includes ``feedback`` even for
        # feedback-gated actions; the agent merges it in after
        # collecting user input.
        args = {**base_args, "action": action}
        requires_feedback = action in _HANDOFF_FEEDBACK_ACTIONS
        # ``halt`` is terminal — no resume. Every other known verb
        # advances the run via resume after the decision is recorded.
        followup = None if action == "halt" else resume_followup
        elicitation = (
            HandoffElicitationHint(
                field="feedback",
                message=_HANDOFF_FEEDBACK_PLACEHOLDER,
                requested_schema=_HANDOFF_FEEDBACK_ELICITATION_SCHEMA,
            )
            if requires_feedback
            else None
        )
        out.append(HandoffDecisionChoice(
            label=f"{_HANDOFF_ACTION_DESCRIPTIONS[action]}",
            action=action,
            tool=_HANDOFF_DECISION_TOOL,
            args=args,
            requires_feedback=requires_feedback,
            feedback_field="feedback" if requires_feedback else None,
            feedback_placeholder=(
                _HANDOFF_FEEDBACK_PLACEHOLDER if requires_feedback else None
            ),
            followup=followup,
            elicitation=elicitation,
        ))
    return out


def _coerce_finding_item(item: object) -> HandoffFindingSummary | None:
    """Convert one raw finding (dict or SDK object) into a bounded
    ``HandoffFindingSummary``. Returns ``None`` only when no usable
    title can be extracted — pure-string items still get a generic
    ``"Finding"`` title so the user is never silently shown an empty
    line."""
    if item is None:
        return None

    def _get(name: str) -> object:
        if isinstance(item, dict):
            return item.get(name)
        return getattr(item, name, None)

    raw_id = _get("id")
    severity = _truncate(_get("severity"), _HANDOFF_FINDING_FILE_MAX)
    # Title precedence: ``title`` > ``summary`` > generic fallback.
    title = (
        _truncate(_get("title"), _HANDOFF_FINDING_TITLE_MAX)
        or _truncate(_get("summary"), _HANDOFF_FINDING_TITLE_MAX)
    )
    if title is None and not isinstance(item, dict) and not hasattr(item, "__dict__"):
        # Pure-string item — surface it as a title.
        s = str(item)
        title = s[:_HANDOFF_FINDING_TITLE_MAX] if s else None
    if title is None:
        title = "Finding"

    body = (
        _truncate(_get("body"), _HANDOFF_FINDING_BODY_MAX)
        or _truncate(_get("message"), _HANDOFF_FINDING_BODY_MAX)
    )
    required_fix = _truncate(_get("required_fix"), _HANDOFF_FINDING_FIX_MAX)
    file_ = _truncate(_get("file"), _HANDOFF_FINDING_FILE_MAX)
    raw_line = _get("line")
    line = raw_line if isinstance(raw_line, int) else None

    return HandoffFindingSummary(
        id=str(raw_id) if raw_id else None,
        severity=severity,
        title=title,
        body=body,
        required_fix=required_fix,
        file=file_,
        line=line,
    )


def _compact_handoff_findings(
    raw_findings: list[object],
) -> list[HandoffFindingSummary]:
    """Compact up to 5 findings for the handoff hint (presentation only).

    Takes the already-resolved findings *source* from the projected
    read-model (``HandoffReadModel.raw_findings`` — see
    ``services.run_projection``) and applies the bounded compaction:
    per-field truncation via :func:`_coerce_finding_item` and the
    5-item limit. Source resolution (meta vs SDK fallback) lives in the
    projector, not here.
    """
    out: list[HandoffFindingSummary] = []
    for item in raw_findings:
        if len(out) >= _HANDOFF_FINDINGS_LIMIT:
            break
        compact = _coerce_finding_item(item)
        if compact is not None:
            out.append(compact)
    return out


def _findings_summary(
    findings: list[HandoffFindingSummary],
) -> str | None:
    """One-line ``"P1: Title; P2: Title"`` aggregate of compact findings.

    ``None`` when no findings; capped at 500 chars defensively even
    though each title is already bounded.
    """
    if not findings:
        return None
    parts: list[str] = []
    for f in findings:
        if f.severity:
            parts.append(f"{f.severity}: {f.title}")
        else:
            parts.append(f.title)
    text = "; ".join(parts)
    if len(text) > _HANDOFF_FINDINGS_SUMMARY_MAX:
        text = text[: _HANDOFF_FINDINGS_SUMMARY_MAX - 1] + "…"
    return text


# Closing-line variants keyed by client. Presentation-only; the action
# list above is identical across clients so correctness stays decoupled
# from rendering.
_HANDOFF_RENDER_HINTS: dict[str, str] = {
    "claude-code": "Present these options clearly and wait for the user's choice.",
    "codex": "Present this as a structured Ask prompt with one required action choice.",
    "generic": "Ask the user to choose one action.",
}


def _recommended_handoff_prompt(
    run_id: str,
    phase: str | None,
    actions: list[str],
    findings: list[HandoffFindingSummary],
    interaction_client: str = "generic",
    *,
    trigger: str | None = None,
    incomplete_count: int = 0,
) -> str:
    """Render the bounded structured-choice prompt the agent shows the user.

    Includes only actions present in ``available_actions``; never invents
    capabilities. Hard-capped at 1500 chars — the agent owns the actual
    conversation, this is just the seed prompt.

    The header line is built by ``_handoff_pause_summary`` — for an
    ``implement``/``incomplete`` pause it reads "paused at implement: N
    subtask(s) incomplete"; otherwise the generic "paused at <phase>".

    ``interaction_client`` only varies the closing render-hint line
    (``Ask`` vs chat vs generic) — the action list and clarification
    instruction stay identical so correctness is independent of the
    selected profile. Unknown clients use the ``generic`` line.
    """
    lines: list[str] = [
        _handoff_pause_summary(run_id, phase, trigger, incomplete_count),
        "",
    ]

    if findings:
        lines.append("Reviewer found:")
        for idx, f in enumerate(findings, start=1):
            prefix = f"{idx}. "
            if f.severity:
                prefix += f"[{f.severity}] "
            line = prefix + f.title
            if f.required_fix:
                line += f" — fix: {f.required_fix}"
            lines.append(line)
    else:
        lines.append(
            "No compact findings were available; inspect "
            "orcho_run_evidence(slice='findings') if needed.",
        )

    lines.append("")
    if actions:
        lines.append("Choose one:")
        for a in actions:
            desc = _HANDOFF_ACTION_DESCRIPTIONS.get(a)
            if desc:
                lines.append(f"- {a}: {desc}")
            else:
                lines.append(f"- {a}")
        feedback_actions = [a for a in actions if a in _HANDOFF_FEEDBACK_ACTIONS]
        if feedback_actions:
            lines.append("")
            for a in feedback_actions:
                lines.append(f"If {a}, provide feedback text.")
    else:
        lines.append(
            "No structured actions are available; inspect orcho_run_status "
            "for the pause payload.",
        )

    lines.append("")
    lines.append(
        "If the user's answer is ambiguous, ask one clarifying question "
        "before calling the decision tool.",
    )

    render_hint = _HANDOFF_RENDER_HINTS.get(
        interaction_client, _HANDOFF_RENDER_HINTS["generic"],
    )
    lines.append(render_hint)

    prompt = "\n".join(lines)
    if len(prompt) > _HANDOFF_PROMPT_MAX:
        prompt = prompt[: _HANDOFF_PROMPT_MAX - 1] + "…"
    return prompt


def build_handoff_hint(
    run_id: str,
    snap: RunEventsSummary,
    *,
    interaction_client: str = "generic",
) -> HandoffDecisionHint | None:
    """Synthesise an enriched ``HandoffDecisionHint`` from meta when paused.

    Populates ``findings`` (with SDK fallback), ``default_action``,
    ``feedback_required_for``, ``findings_summary``, ``client_hints``,
    and a structured-choice ``recommended_user_prompt``. The agent
    consumes this and runs the conversation; Orcho stays bounded and
    never reads stdin.

    ``interaction_client`` selects a presentation profile (``generic`` /
    ``claude-code`` / ``codex``) that shapes ``client_hints`` and the
    closing render-hint line of the prompt. Unknown values resolve to
    ``generic`` (see ``get_interaction_profile``). Correctness —
    ``available_actions``, ``default_action``, ``feedback_required_for``,
    ``handoff_id`` — is invariant under the profile.

    Defensive: malformed meta must never raise Pydantic validation at
    the exact moment the user needs the decision prompt. The
    ``meta.phase_handoff`` read-model is normalised by the services
    projector (which coerces junk to safe defaults and swallows the
    findings-fallback SDK errors); this builder only renders it.
    """
    if snap.status not in _HANDOFF_STATUSES:
        return None

    read_model = project_handoff_read_model(
        run_id, current_phase=snap.current_phase,
    )
    # A recorded decision has no remaining decide form; a degraded read must
    # not be mistaken for a fresh decision.  The compact pending projection
    # still reports the safe next step.
    if read_model.decision_state != "missing":
        return None
    actions = read_model.actions
    handoff_id = read_model.handoff_id
    phase = read_model.phase
    trigger = read_model.trigger
    incomplete_count = read_model.incomplete_count

    findings = _compact_handoff_findings(read_model.raw_findings)
    findings_summary = _findings_summary(findings)
    default_action = _handoff_default_action(actions)
    feedback_required_for = _handoff_feedback_required_for(actions)
    choices = _build_choices(run_id, handoff_id, actions)

    # Profile selection is presentation-only. Resolved-client below is
    # whatever ``get_interaction_profile`` returns — unknown inputs
    # normalise to ``generic`` here, so the render hint and the
    # ``client_hints.client`` label stay consistent with each other.
    profile = get_interaction_profile(interaction_client)
    client_hints = profile_to_client_hints(profile)

    if phase == "implement" and trigger == "incomplete":
        noun = "subtask" if incomplete_count == 1 else "subtasks"
        title = (
            f"implement: {incomplete_count} {noun} incomplete "
            "requires a decision"
        )
    else:
        label = phase or "run"
        title = f"{label} requires a decision"
    prompt = _recommended_handoff_prompt(
        run_id, phase, actions, findings,
        interaction_client=profile.client,
        trigger=trigger,
        incomplete_count=incomplete_count,
    )

    return HandoffDecisionHint(
        run_id=run_id,
        handoff_id=handoff_id,
        phase=phase,
        title=title,
        findings_summary=findings_summary,
        findings=findings,
        available_actions=actions,
        default_action=default_action,
        feedback_required_for=feedback_required_for,
        recommended_user_prompt=prompt,
        client_hints=client_hints,
        choices=choices,
    )


__all__ = ["build_handoff_hint"]
