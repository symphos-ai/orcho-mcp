"""orcho_mcp.observe.watch — long-poll watch loop for orcho_run_watch.

Backs the ``orcho_run_watch`` MCP tool. Holds the request open until
the chosen ``until`` condition fires or ``timeout_s`` expires.
Designed to replace manual re-polling of ``orcho_run_status`` /
``orcho_run_events_summary`` during long phases (implement, repair)
where minutes can pass between user-relevant events.

When the MCP request carries a ``progressToken``, ordered
``notifications/progress`` are emitted as the event sequence advances
(one notification per observed seq advance, not per individual event).
FastMCP no-ops ``ctx.report_progress`` when no progressToken is set —
this module just passes ``ctx`` through, never reaches into
``ctx.request_context.meta``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import Context

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.observe.handoff_hints import build_handoff_hint
from orcho_mcp.observe.summary import build_run_events_summary
from orcho_mcp.schemas import RunEventsSummary, RunWatchResult, WatchTrigger
from orcho_mcp.services.run_events import read_run_events
from orcho_mcp.services.run_lookup import find_run_dir

# ── Watch constants ─────────────────────────────────────────────────────────
#
# ``orcho_run_watch`` holds the MCP request open until a meaningful trigger
# fires or ``timeout_s`` expires. Advanced-profile runs in the wild routinely
# spend 30–40 minutes between user-relevant events, so the timeout ceiling
# is generous; the poll interval stays small because the cost of an
# extra SDK event read against a JSONL file is negligible.

_WATCH_POLL_INTERVAL_S = 0.25
_WATCH_TIMEOUT_CEILING = 7200  # 2h
_WATCH_TERMINAL_STATUSES = {
    "done", "failed", "interrupted", "halted", "orphaned",
}
_WATCH_HANDOFF_STATUSES = {
    "awaiting_phase_handoff", "awaiting_gate_decision",
}
_WATCH_UNTIL_CHOICES = {
    "next_event", "phase_change", "subtask", "handoff_or_terminal", "terminal",
}
_WATCH_PROGRESS_MESSAGE_MAX = 200


def _current_phase_at_seq(run_dir: Path, seq: int) -> str | None:
    """Phase active at ``seq``, computed by walking all events up to and
    including ``seq``.

    ``phase.start`` sets the current phase; ``phase.end`` clears it back to
    ``None``. Returns ``None`` for ``seq <= 0`` or when no phase has opened.

    Used by ``watch_run`` to compute the *baseline* phase at the caller's
    ``since_seq`` — distinct from ``RunEventsSummary.current_phase``,
    which walks up to ``next_seq``. The baseline is the phase the caller
    last saw; the summary's current_phase is the phase right now. Using
    the summary value as baseline would mask phase changes that already
    happened between the caller's last poll and this watch call.
    """
    if seq <= 0:
        return None
    current: str | None = None
    for evt in read_run_events(run_dir.name):
        if evt.seq > seq:
            break
        if evt.kind == "phase.start":
            current = evt.phase
        elif evt.kind == "phase.end":
            current = None
    return current


def _progress_message(snap: RunEventsSummary) -> str:
    """Short bounded progress text for ``notifications/progress``.

    Never quotes raw event payload text. Hard-capped at 200 chars
    defensively even though the inputs are already short by convention.
    """
    if snap.status in _WATCH_HANDOFF_STATUSES:
        msg = f"paused: {snap.status}"
    elif snap.status in _WATCH_TERMINAL_STATUSES:
        msg = f"terminal: {snap.status}"
    else:
        phase = snap.current_phase or "(unknown)"
        cs = snap.current_subtask
        if cs is not None and cs.total:
            # "implement: subtask 3/12 done (Patch target)" — the step-by-step
            # progress line for a long subtask_dag implement phase.
            label = f"subtask {cs.index}/{cs.total}"
            if cs.state and cs.state != "running":
                label += f" {cs.state}"
            if cs.goal:
                label += f" ({cs.goal})"
            msg = f"running: {phase} — {label}"
        else:
            msg = f"running: {phase} at seq {snap.next_seq}"
    if len(msg) > _WATCH_PROGRESS_MESSAGE_MAX:
        msg = msg[: _WATCH_PROGRESS_MESSAGE_MAX - 1] + "…"
    return msg


async def _maybe_report_watch_progress(
    ctx: Context | None,
    snap: RunEventsSummary,
    last_reported_seq: int,
) -> int:
    """Emit a ``notifications/progress`` only on seq advance.

    Single choke-point for "progress on event advance" so the invariant
    cannot drift across fast-path / loop / timeout return paths.
    ``Context.report_progress`` is itself a no-op when the request carries
    no ``progressToken``, but skipping the call entirely when nothing has
    advanced also keeps the test surface honest.
    """
    if ctx is None:
        return last_reported_seq
    if snap.next_seq <= last_reported_seq:
        return last_reported_seq
    await ctx.report_progress(
        progress=float(snap.next_seq),
        total=None,
        message=_progress_message(snap),
    )
    return snap.next_seq


def _watch_should_trigger(
    until: str,
    baseline_seq: int,
    baseline_phase: str | None,
    snap: RunEventsSummary,
) -> WatchTrigger | None:
    """Return a populated ``WatchTrigger`` when ``snap`` satisfies ``until``,
    else ``None``.

    Pure function: no IO, no time, no progress — caller drives the loop
    and the progress notification.
    """
    status = snap.status
    phase = snap.current_phase
    seq = snap.next_seq

    if until == "next_event":
        if seq > baseline_seq:
            return WatchTrigger(
                kind="next_event",
                reason=f"new event at seq {seq}",
                seq=seq, status=status, phase=phase,
            )
        return None

    if until == "phase_change":
        # Handoff / terminal are strictly more informative than a bare phase
        # transition, so they override even on the phase_change path.
        if status in _WATCH_HANDOFF_STATUSES:
            return WatchTrigger(
                kind="handoff", reason=f"status={status}",
                seq=seq, status=status, phase=phase,
            )
        if status in _WATCH_TERMINAL_STATUSES:
            return WatchTrigger(
                kind="terminal", reason=f"status={status}",
                seq=seq, status=status, phase=phase,
            )
        # No ``is not None`` guard: a phase that ended (current_phase
        # transitions to ``None``) is still a phase change worth surfacing.
        if phase != baseline_phase:
            return WatchTrigger(
                kind="phase_change",
                reason=f"phase {baseline_phase!r} -> {phase!r}",
                seq=seq, status=status, phase=phase,
            )
        return None

    if until == "subtask":
        # Step-by-step cadence for a long subtask_dag implement phase: wake on
        # each subtask boundary (start or end). Handoff / terminal still
        # override so the loop ends with the run rather than hanging past it.
        if status in _WATCH_HANDOFF_STATUSES:
            return WatchTrigger(
                kind="handoff", reason=f"status={status}",
                seq=seq, status=status, phase=phase,
            )
        if status in _WATCH_TERMINAL_STATUSES:
            return WatchTrigger(
                kind="terminal", reason=f"status={status}",
                seq=seq, status=status, phase=phase,
            )
        cs = snap.current_subtask
        if cs is not None and cs.seq > baseline_seq:
            return WatchTrigger(
                kind="subtask",
                reason=f"subtask {cs.index}/{cs.total} {cs.state}",
                seq=seq, status=status, phase=phase,
            )
        return None

    if until == "handoff_or_terminal":
        if status in _WATCH_HANDOFF_STATUSES:
            return WatchTrigger(
                kind="handoff", reason=f"status={status}",
                seq=seq, status=status, phase=phase,
            )
        if status in _WATCH_TERMINAL_STATUSES:
            return WatchTrigger(
                kind="terminal", reason=f"status={status}",
                seq=seq, status=status, phase=phase,
            )
        return None

    if until == "terminal":
        if status in _WATCH_TERMINAL_STATUSES:
            return WatchTrigger(
                kind="terminal", reason=f"status={status}",
                seq=seq, status=status, phase=phase,
            )
        return None

    return None


def _watch_take_snapshot(
    run_id: str, run_dir: Path, since_seq: int,
) -> RunEventsSummary:
    """Take a bounded-summary snapshot anchored to the *latest* event seq.

    Why the indirection: ``build_run_events_summary`` takes a literal
    ``(since_seq, limit)`` window. If the watch passed the caller's
    ``since_seq`` verbatim on every poll, more than ``limit`` events
    accumulating past the cursor would pin ``next_seq`` to ``since_seq +
    limit`` and silently mask everything later — including a
    ``phase.start`` that fires the ``phase_change`` trigger.

    Instead, this helper computes ``windowed_since = max(since_seq,
    latest_seq - 200)`` so the snapshot's window always slides to cover
    the last 200 events in the stream. The bounded-payload guarantee is
    preserved, and:

    - ``snap.next_seq`` always equals the run's true latest seq, so
      ``next_event`` triggers and progress notifications keep advancing;
    - ``snap.current_phase`` is computed by walking *all* events up to
      ``snap.next_seq``, so ``phase_change`` triggers detect phase
      transitions wherever they land in the stream.

    Reads the SDK-backed event stream once locally to find ``latest_seq``;
    ``build_run_events_summary`` reads it again internally. Two reads
    per poll against a small JSONL file is well under our latency
    budget; tail-incremental reads belong behind this helper if
    profiling ever shows them necessary.

    Calls ``build_run_events_summary`` (service entry) — never the
    ``orcho_run_events_summary`` @mcp.tool shim. Observe must not cycle
    back through the wire adapter layer.
    """
    all_events = read_run_events(run_id)
    latest_seq = all_events[-1].seq if all_events else since_seq
    windowed_since = max(since_seq, latest_seq - 200)
    return build_run_events_summary(
        run_id, since_seq=windowed_since, limit=200, last_n=5,
    )


def _build_watch_result(
    run_id: str,
    trigger: WatchTrigger,
    snap: RunEventsSummary,
    *,
    want_summary: bool,
    interaction_client: str = "generic",
    triggered: bool = True,
) -> RunWatchResult:
    """Common return-shape builder for ``watch_run``.

    ``interaction_client`` is forwarded to ``build_handoff_hint`` only;
    it has no effect on ``trigger``, ``summary``, or ``triggered``.
    """
    handoff = (
        build_handoff_hint(
            run_id, snap, interaction_client=interaction_client,
        )
        if trigger.kind == "handoff"
        else None
    )
    return RunWatchResult(
        run_id=run_id,
        triggered=triggered,
        trigger=trigger,
        summary=snap if want_summary else None,
        handoff=handoff,
    )


async def watch_run(
    run_id: str,
    since_seq: int = 0,
    until: Literal[
        "next_event",
        "phase_change",
        "subtask",
        "handoff_or_terminal",
        "terminal",
    ] = "handoff_or_terminal",
    timeout_s: int = 3600,
    summary: bool = True,
    interaction_client: str = "generic",
    ctx: Context | None = None,
) -> RunWatchResult:
    """Long-poll a run until something meaningful happens.

    Service entry behind ``orcho_run_watch`` MCP tool. See the tool's
    docstring (in ``orcho_mcp.tools``) for the wire contract. Defaults
    match the wire defaults exactly — do not change without
    regenerating ``docs/mcp_schema.json``.
    """
    # Input validation — defensive even though Literal narrows ``until``.
    if since_seq < 0:
        raise InvalidPlanError(
            f"orcho_run_watch: since_seq must be >= 0, got {since_seq}",
        )
    if timeout_s <= 0 or timeout_s > _WATCH_TIMEOUT_CEILING:
        raise InvalidPlanError(
            f"orcho_run_watch: timeout_s must be in "
            f"(0, {_WATCH_TIMEOUT_CEILING}], got {timeout_s}",
        )
    if until not in _WATCH_UNTIL_CHOICES:
        # Belt-and-suspenders — Literal already rejects this at the wire,
        # but pure-Python callers in L1 tests bypass the schema.
        raise InvalidPlanError(
            f"orcho_run_watch: until must be one of "
            f"{sorted(_WATCH_UNTIL_CHOICES)}, got {until!r}",
        )

    # Resolve once up front so RunNotFoundError surfaces immediately.
    run_dir = find_run_dir(run_id)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s

    # Baseline phase must come from the event stream *up to since_seq*,
    # not from the initial summary's ``current_phase`` (which is computed
    # at next_seq and would mask an already-happened phase change on
    # reconnect).
    baseline_phase = _current_phase_at_seq(run_dir, since_seq)
    last_reported_seq = since_seq

    # Use the dynamic-window helper, not a raw ``build_run_events_summary``
    # call with the caller's ``since_seq``: a fixed lower bound + the
    # 200-event ``limit`` would silently freeze ``next_seq`` once more
    # than 200 events accumulate past the cursor, masking phase changes
    # and stalling progress notifications. See ``_watch_take_snapshot``.
    initial = _watch_take_snapshot(run_id, run_dir, since_seq)

    # Fast-path: trigger condition already true on entry.
    fast = _watch_should_trigger(until, since_seq, baseline_phase, initial)
    if fast is not None:
        # Emit one progress notification *before* the fast-path return so
        # progressToken-bearing clients always see ≥ 1 event when there is
        # anything new past the cursor.
        last_reported_seq = await _maybe_report_watch_progress(
            ctx, initial, last_reported_seq,
        )
        return _build_watch_result(
            run_id, fast, initial,
            want_summary=summary, interaction_client=interaction_client,
        )

    while loop.time() < deadline:
        await asyncio.sleep(_WATCH_POLL_INTERVAL_S)
        snap = _watch_take_snapshot(run_id, run_dir, since_seq)

        last_reported_seq = await _maybe_report_watch_progress(
            ctx, snap, last_reported_seq,
        )

        trig = _watch_should_trigger(until, since_seq, baseline_phase, snap)
        if trig is not None:
            return _build_watch_result(
                run_id, trig, snap,
                want_summary=summary, interaction_client=interaction_client,
            )

    # Timeout path: one last snapshot so the bounded summary reflects the
    # latest state the client can see.
    final = _watch_take_snapshot(run_id, run_dir, since_seq)
    last_reported_seq = await _maybe_report_watch_progress(
        ctx, final, last_reported_seq,
    )
    timeout_trigger = WatchTrigger(
        kind="timeout",
        reason="timeout_s expired",
        seq=final.next_seq,
        status=final.status,
        phase=final.current_phase,
    )
    return _build_watch_result(
        run_id, timeout_trigger, final,
        want_summary=summary, interaction_client=interaction_client,
        triggered=False,
    )


__all__ = ["watch_run"]
