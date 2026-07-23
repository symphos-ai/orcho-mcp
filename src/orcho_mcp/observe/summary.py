"""orcho_mcp.observe.summary — bounded events-summary builder.

Backs the ``orcho_run_events_summary`` MCP tool. The agent's typical
question while a run is in flight is "what changed and what should I
do next?" — not "give me every raw event." This module answers the
first question in a single bounded payload, so status polling no
longer triggers MCP tool-result auto-spill on the client side.

Bounded ceilings (constants below) refuse pathological inputs at the
wire — a long-tail bug in the operator-side LLM cannot turn the
summary into a 200K-event dump just by passing ``limit=999999``. The
truncation caps keep ``CompactRunEvent`` projection within budget.
"""
from __future__ import annotations

from sdk import load_meta

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.observe.observation import record_workspace_observation
from orcho_mcp.schemas import (
    CompactRunEvent,
    CurrentSubtaskRecord,
    PendingHandoffSummary,
    PhaseEventSummary,
    ProviderSessionFallback,
    RetryState,
    RunEventsSummary,
)
from orcho_mcp.services.run_events import read_run_events
from orcho_mcp.services.run_lookup import find_run_dir
from orcho_mcp.services.run_projection import (
    build_provider_pressure,
    is_provider_session_fallback_event,
    merged_status_from_meta,
    project_pending_handoff,
    project_provider_pressure,
    project_provider_session_fallback,
    project_retry_state,
)

# ── Bounded-observe constants ─────────────────────────────────────────────────
#
# Default summaries stay tight for live agent status reads. The higher
# ceiling remains available for explicit forensic calls.

_SUMMARY_DEFAULT_LIMIT = 50
_SUMMARY_LIMIT_CEILING = 1000
_SUMMARY_LAST_N_CEILING = 100

# Per-field truncation caps for ``CompactRunEvent``. ``summary`` is the
# human-readable extract and gets a larger budget; everything else is
# orcho-controlled and short by convention — capped only as a defensive
# measure so the 200-event ≤ 10 KB assertion stays honest if the emitter
# vocabulary grows.
_COMPACT_SUMMARY_MAX = 256
_COMPACT_FIELD_MAX = 64

# Preview cap for the reviewer's last output inside the pending-handoff
# projection. Larger than the per-event field caps (the reviewer critique
# is the one place an operator wants real context at a glance) but still
# bounded so a pathological critique cannot inflate the summary payload.
_PENDING_HANDOFF_LAST_OUTPUT_MAX = 500

# Preview cap for the operator's retry feedback inside the retry-state
# projection. Same budget reasoning as the pending-handoff last-output
# preview.
_RETRY_FEEDBACK_PREVIEW_MAX = 500

# The single-project pause status that carries a pending phase-handoff
# decision payload. Mirrors the projection owner's constant; kept local
# so summary stays self-contained.
_PENDING_HANDOFF_STATUS = "awaiting_phase_handoff"

# Defensive cap on the number of provider-session fallback records carried
# in a summary. These events are rare (one per phase that hit a stale
# provider session and recovered); the cap keeps the bounded-observe
# budget honest even against a pathological event stream.
_PROVIDER_SESSION_FALLBACKS_MAX = 25


def _truncate(value: object, limit: int) -> str | None:
    """Coerce to str + cap at ``limit`` chars. ``None`` / empty → ``None``."""
    if value is None:
        return None
    s = str(value)
    if not s:
        return None
    return s if len(s) <= limit else s[:limit]


def _compact_event(evt) -> CompactRunEvent:
    """Project a raw ``Event`` into a bounded ``CompactRunEvent``.

    Pulls a small set of well-known payload keys and drops everything
    else. The 256-char summary cap and 64-char per-field caps are what
    bound the wire payload of a 200-event summary; the L1 test
    ``test_events_summary_bounded_payload`` proves the budget holds
    against a synthetic 10 KB ``payload["command"]`` per event.
    """
    payload = evt.payload or {}
    # Pick the first non-empty among the canonical summary keys.
    summary: str | None = None
    for key in ("summary", "text", "message"):
        candidate = payload.get(key)
        if candidate:
            summary = _truncate(candidate, _COMPACT_SUMMARY_MAX)
            break
    tool = _truncate(
        payload.get("tool") or payload.get("tool_name"),
        _COMPACT_FIELD_MAX,
    )
    status = _truncate(payload.get("status"), _COMPACT_FIELD_MAX)
    return CompactRunEvent(
        seq=evt.seq,
        ts=evt.ts,
        # ``kind`` is required on the schema; even truncated, give back a
        # string. ``phase`` may legitimately be None.
        kind=_truncate(evt.kind, _COMPACT_FIELD_MAX) or "",
        phase=_truncate(evt.phase, _COMPACT_FIELD_MAX),
        summary=summary,
        tool=tool,
        status=status,
    )


# Conservative, data-driven next-action hints keyed off the merged status.
# No invention — empty list for unknown status so callers can't be misled
# into following a guessed step. Kept as plain strings to preserve the
# compact summary shape.
_NEXT_ACTIONS_BY_STATUS: dict[str, list[str]] = {
    "running": [
        "poll orcho_run_events_summary again for progress",
        "use orcho_run_status for the authoritative state",
    ],
    "awaiting_phase_handoff": [
        "inspect orcho_run_status for the handoff payload",
        "decide via orcho_phase_handoff_decide",
        "then orcho_run_resume to continue",
    ],
    "awaiting_gate_decision": [
        "inspect orcho_run_status for the gate payload",
        "then orcho_run_resume to continue",
    ],
    "done": [
        "inspect orcho_run_evidence for findings",
        "inspect orcho_run_diff for changes",
        "inspect orcho_run_metrics for cost",
    ],
}

_TERMINAL_FAILURE_STATES = ("failed", "interrupted", "halted", "orphaned")


def _subtask_end_state(payload: dict) -> str:
    """Map a ``subtask.end`` payload to a terminal subtask state label.

    ``incomplete`` (done-criteria attestation gate did not close) is checked
    before ``failed`` so an attested-but-incomplete subtask is not mislabeled
    as a hard execution failure.
    """
    if payload.get("attestation_error"):
        return "incomplete"
    if payload.get("ok") is False or payload.get("error"):
        return "failed"
    return "done"


def _subtask_record(evt, *, state: str) -> CurrentSubtaskRecord:
    """Project a ``subtask.start`` / ``subtask.end`` event into the live
    progress coordinate. Each such event self-describes (index/total/goal),
    so no cross-event matching is needed."""
    p = evt.payload or {}
    return CurrentSubtaskRecord(
        subtask_id=str(p.get("subtask_id") or ""),
        index=int(p.get("index") or 0),
        total=int(p.get("total") or 0),
        goal=_truncate(p.get("goal"), _COMPACT_SUMMARY_MAX) or "",
        state=state,
        seq=evt.seq,
    )


def _summary_next_actions(
    status: str | None, pending: PendingHandoffSummary | None = None,
) -> list[str]:
    """Return short imperative next-action strings for the resolved status."""
    if status is None:
        return []
    if status == _PENDING_HANDOFF_STATUS and pending is not None:
        if pending.decision_state == "recorded":
            return ["resume via orcho_run_resume to apply the recorded decision"]
        if pending.decision_state == "degraded":
            return ["inspect orcho_run_diagnose; the handoff decision could not be read"]
    if status in _NEXT_ACTIONS_BY_STATUS:
        return list(_NEXT_ACTIONS_BY_STATUS[status])
    if status in _TERMINAL_FAILURE_STATES:
        return [
            "inspect orcho_run_evidence for errors",
            "inspect orcho_run_metrics for what completed",
        ]
    return []


def _build_pending_handoff(
    run_id: str, status: str | None, current_phase: str | None,
) -> PendingHandoffSummary | None:
    """Project the active phase-handoff into a compact summary field.

    Returns ``None`` for any non-paused status so the hot polling path
    skips the extra meta read. When paused, the services projection owns
    the meta/payload read + round-label derivation; this function only
    truncates ``last_output`` into a bounded preview (presentation).
    """
    if status != _PENDING_HANDOFF_STATUS:
        return None
    pending = project_pending_handoff(run_id, current_phase=current_phase)
    if not pending.is_pending_handoff:
        return None
    return PendingHandoffSummary(
        handoff_id=pending.handoff_id,
        phase=pending.phase,
        trigger=pending.trigger,
        verdict=pending.verdict,
        round_label=pending.round_label,
        available_actions=list(pending.available_actions),
        last_output_preview=_truncate(
            pending.last_output, _PENDING_HANDOFF_LAST_OUTPUT_MAX,
        ),
        decision_artifact_exists=pending.decision_artifact_exists,
        decision_state=pending.decision_state,
        decision_degraded_reason=pending.decision_degraded_reason,
        suggested_next_action=pending.suggested_next_action,
    )


def _build_retry_state(
    run_id: str, current_phase: str | None,
) -> RetryState | None:
    """Project the human-retry / repeated-reject lifecycle into a wire field.

    The services projection owns the meta/decision read + classification;
    this function only truncates the raw operator feedback into a bounded
    preview (presentation). Returns ``None`` when the run is not in a
    reject / retry lifecycle.
    """
    retry = project_retry_state(run_id, current_phase=current_phase)
    if retry is None:
        return None
    return RetryState(
        retry_context=retry.retry_context,
        retry_attempt_label=retry.retry_attempt_label,
        operator_feedback_preview=_truncate(
            retry.operator_feedback, _RETRY_FEEDBACK_PREVIEW_MAX,
        ),
        pending_operator_decision=retry.pending_operator_decision,
    )


def build_run_events_summary(
    run_id: str,
    since_seq: int = 0,
    limit: int = _SUMMARY_DEFAULT_LIMIT,
    last_n: int = 5,
) -> RunEventsSummary:
    """Return a bounded summary of a run's recent events.

    Backs the ``orcho_run_events_summary`` MCP tool. See the tool's
    docstring (in ``orcho_mcp.tools``) for the wire contract; the
    implementation below is the canonical behaviour and the MCP shim
    is a one-line delegation.
    """
    # Input validation — keep the bounded tool actually bounded.
    if since_seq < 0:
        raise InvalidPlanError(
            f"orcho_run_events_summary: since_seq must be >= 0, got {since_seq}",
        )
    if limit <= 0 or limit > _SUMMARY_LIMIT_CEILING:
        raise InvalidPlanError(
            f"orcho_run_events_summary: limit must be in "
            f"(0, {_SUMMARY_LIMIT_CEILING}], got {limit}",
        )
    if last_n < 0 or last_n > _SUMMARY_LAST_N_CEILING:
        raise InvalidPlanError(
            f"orcho_run_events_summary: last_n must be in "
            f"[0, {_SUMMARY_LAST_N_CEILING}], got {last_n}",
        )

    run_dir = find_run_dir(run_id)
    all_events = read_run_events(run_id)

    new_events = [e for e in all_events if e.seq > since_seq]
    windowed = new_events[:limit]

    next_seq = windowed[-1].seq if windowed else since_seq
    eof = len(new_events) <= limit

    # ── current_phase: scan ALL events up to next_seq ────────────────────────
    # Walking the windowed slice would lose phase context the moment a
    # poll lands a window that starts inside a phase. ``next_seq`` is
    # the right horizon — anything later belongs to the NEXT batch and
    # must not influence the "current" answer either.
    current_phase: str | None = None
    current_subtask: CurrentSubtaskRecord | None = None
    provider_session_fallbacks: list[ProviderSessionFallback] = []
    for evt in all_events:
        if evt.seq > next_seq:
            break
        if evt.kind == "phase.start":
            current_phase = evt.phase
        elif evt.kind == "phase.end":
            current_phase = None
            # The implement phase ending closes any in-flight subtask: a stale
            # "subtask 12/12" must not linger into review/final_acceptance.
            current_subtask = None
        elif evt.kind == "subtask.start":
            current_subtask = _subtask_record(evt, state="running")
        elif evt.kind == "subtask.end":
            current_subtask = _subtask_record(
                evt, state=_subtask_end_state(evt.payload or {}),
            )
        elif (
            is_provider_session_fallback_event(evt.kind)
            and len(provider_session_fallbacks) < _PROVIDER_SESSION_FALLBACKS_MAX
        ):
            # Recovered missing-provider-session fallback — a recovery
            # notice, NOT a phase failure. Projected to structured fields
            # (phase_succeeded=True) so the merged status below is never
            # degraded by its presence.
            provider_session_fallbacks.append(
                ProviderSessionFallback(
                    **vars(project_provider_session_fallback(evt.payload or {})),
                ),
            )

    # ── counts / by_kind / by_phase: windowed only ───────────────────────────
    by_kind_counts: dict[str, int] = {}
    # Preserve first-seen phase order via a parallel ordered key list.
    phase_order: list[str] = []
    phase_buckets: dict[str, dict[str, object]] = {}

    for evt in windowed:
        by_kind_counts[evt.kind] = by_kind_counts.get(evt.kind, 0) + 1
        phase_key = evt.phase or "(unknown)"
        bucket = phase_buckets.get(phase_key)
        if bucket is None:
            bucket = {"count": 0, "kinds": set()}
            phase_buckets[phase_key] = bucket
            phase_order.append(phase_key)
        bucket["count"] = int(bucket["count"]) + 1  # type: ignore[arg-type]
        kinds_set = bucket["kinds"]
        assert isinstance(kinds_set, set)
        kinds_set.add(evt.kind)

    by_phase = [
        PhaseEventSummary(
            phase=phase_key,
            count=int(phase_buckets[phase_key]["count"]),  # type: ignore[arg-type]
            kinds=sorted(phase_buckets[phase_key]["kinds"]),  # type: ignore[arg-type]
        )
        for phase_key in phase_order
    ]

    # ── last_n: explicit guard against ``windowed[-0:]`` whole-list bug ──────
    tail = [] if last_n == 0 else windowed[-last_n:]
    last_n_records = [_compact_event(e) for e in tail]

    # ── status + next_actions ────────────────────────────────────────────────
    # Reuse the shared helper so the merge stays consistent with
    # ``orcho_run_status``. We have to load meta.json here because the
    # SDK ``load_status`` would re-resolve the run via find_run; just
    # using ``load_meta`` is cheaper and we already have ``run_dir``.
    meta = load_meta(run_dir) or {}
    status = merged_status_from_meta(meta, run_dir)

    # Provider-pressure (pinned wire shape): for a terminal-failure status,
    # surface the core-typed provider runtime/access failure from the SAME
    # ``project_provider_pressure`` source + shared
    # ``build_provider_pressure_next_actions`` helper as status / diagnose /
    # evidence. ``None`` for a live / clean run or a generic failure. The
    # legacy ``next_actions: list[str]`` field stays untouched — the typed
    # provider-pressure actions ride in ``provider_pressure.next_actions``.
    provider_pressure = (
        build_provider_pressure(project_provider_pressure(run_id))
        if status in _TERMINAL_FAILURE_STATES else None
    )

    pending_handoff = _build_pending_handoff(run_id, status, current_phase)
    result = RunEventsSummary(
        run_id=run_id,
        total_count=len(windowed),
        next_seq=next_seq,
        eof=eof,
        status=status,
        current_phase=current_phase,
        current_subtask=current_subtask,
        by_phase=by_phase,
        by_kind=by_kind_counts,
        last_n=last_n_records,
        next_actions=_summary_next_actions(status, pending_handoff),
        pending_handoff=pending_handoff,
        provider_session_fallbacks=provider_session_fallbacks,
        retry_state=_build_retry_state(run_id, current_phase),
        provider_pressure=provider_pressure,
    )
    # Record the observation in the advisory workspace state. This is
    # best-effort — the helper swallows its own exceptions and logs at
    # debug. ``orcho_run_watch`` routes through this function on every
    # poll (initial + loop + timeout), so the watch path picks up state
    # updates without separate wiring even when ``summary=False``.
    record_workspace_observation(run_id, result)
    return result


def build_latest_run_events_summary(
    run_id: str,
    limit: int = _SUMMARY_DEFAULT_LIMIT,
    last_n: int = 5,
) -> RunEventsSummary:
    """Return a bounded summary anchored to the latest event sequence."""
    if limit <= 0 or limit > _SUMMARY_LIMIT_CEILING:
        raise InvalidPlanError(
            f"orcho_run_events_summary: limit must be in "
            f"(0, {_SUMMARY_LIMIT_CEILING}], got {limit}",
        )

    all_events = read_run_events(run_id)
    latest_seq = all_events[-1].seq if all_events else 0
    since_seq = max(0, latest_seq - limit)
    return build_run_events_summary(
        run_id,
        since_seq=since_seq,
        limit=limit,
        last_n=last_n,
    )


__all__ = [
    "build_latest_run_events_summary",
    "build_run_events_summary",
]
