"""Unit tests for ``orcho_run_watch`` core behavior (no handoff).

Covers next_event/timeout/phase_change/phase_end/terminal triggers,
sliding-window correctness past the 200-event summary page, input
validation, the summary=False contract, and the workspace-state
observation side effect that fires even when summary=False.

Handoff-specific watch behavior lives in ``test_handoff_hints.py``.
"""
from __future__ import annotations

import pytest

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.observe.watch import _WATCH_TIMEOUT_CEILING
from orcho_mcp.tools import orcho_run_watch, orcho_workspace_state
from tests.fixtures.mcp_workspace import write_run


def _ev(seq: int, kind: str = "phase.start", phase: str = "plan", **payload):
    return {"seq": seq, "ts": f"2026-01-01T00:00:{seq:02d}", "kind": kind,
            "phase": phase, "payload": payload}


@pytest.fixture
def anyio_backend():
    # Pin to asyncio — pytest-anyio defaults try trio too, which we don't
    # ship as a dep. Same pattern as test_stdio_read_tools.py.
    return "asyncio"


@pytest.mark.anyio
async def test_watch_next_event_immediate(fake_workspace):
    """until=next_event returns via fast-path when seq advances past
    since_seq. Should return without sleeping the full timeout."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[_ev(1), _ev(2, kind="phase.end"), _ev(3, kind="run.end")],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0, until="next_event", timeout_s=5,
    )
    assert r.triggered is True
    assert r.trigger.kind == "next_event"
    assert r.trigger.seq == 3
    assert r.summary is not None
    assert r.summary.next_seq == 3


@pytest.mark.anyio
async def test_watch_timeout_returns_bounded(fake_workspace):
    """No events past since_seq → timeout path with bounded summary."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[_ev(i) for i in range(1, 4)],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=10, until="next_event", timeout_s=1,
    )
    assert r.triggered is False
    assert r.trigger.kind == "timeout"
    assert r.summary is not None
    # Reconnect cursor: trigger.seq must equal summary.next_seq.
    assert r.trigger.seq == r.summary.next_seq


@pytest.mark.anyio
async def test_watch_phase_change_reconnect_baseline(fake_workspace):
    """Regression: baseline_phase must be computed from the event stream
    up to since_seq, NOT from initial.current_phase. If events past the
    cursor already contain a new phase.start, that is the change to
    surface — the previous implementation derived baseline from the
    initial summary at next_seq and would mask this."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[
            _ev(1, kind="phase.start", phase="plan"),
            _ev(2, kind="phase.end", phase="plan"),
            _ev(3, kind="phase.start", phase="implement"),
        ],
    )
    # Baseline at since_seq=2 is None (plan closed). Events past cursor
    # already include phase.start(implement), so the watch must trigger
    # phase_change on the fast path.
    r = await orcho_run_watch(
        "20260101_000001", since_seq=2, until="phase_change", timeout_s=5,
    )
    assert r.triggered is True
    assert r.trigger.kind == "phase_change"
    assert r.trigger.phase == "implement"


@pytest.mark.anyio
async def test_watch_phase_change_past_summary_page(fake_workspace):
    """Regression: events past the 200-event summary page must still
    drive triggers.

    The bug: ``orcho_run_events_summary(since_seq=0, limit=200)`` returns
    ``next_seq=200`` and computes ``current_phase`` only over events up
    to ``next_seq``. If a ``phase.start`` lands at seq 231 with 250
    events total, the windowed snapshot never sees it and watch waits
    out the full ``timeout_s``.

    The fix (``_watch_take_snapshot``) slides the window so it always
    anchors to ``latest_seq`` — the summary covers events
    ``[max(since_seq, latest_seq - 200), latest_seq]``, and
    ``current_phase`` walks the full stream up to the new ``next_seq``.
    """
    events = [_ev(1, kind="phase.start", phase="plan")]
    # Bulk plan-phase activity past the page boundary.
    for i in range(2, 231):
        events.append(_ev(i, kind="agent.tool_use", phase="plan"))
    # Phase transition lands AFTER the would-be page boundary.
    events.append(_ev(231, kind="phase.end", phase="plan"))
    events.append(_ev(232, kind="phase.start", phase="implement"))
    for i in range(233, 251):
        events.append(_ev(i, kind="agent.tool_use", phase="implement"))

    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=events,
    )

    # Baseline at since_seq=1 is "plan" (phase.start@1). With the page
    # bug, snap.current_phase would still be "plan" (no phase.end visible
    # in the first 200 events past since_seq=1). With the fix, the
    # sliding window's snap.next_seq=250 and current_phase="implement",
    # so phase_change fires fast-path.
    r = await orcho_run_watch(
        "20260101_000001", since_seq=1, until="phase_change", timeout_s=2,
    )
    assert r.triggered is True
    assert r.trigger.kind == "phase_change"
    assert r.trigger.phase == "implement"
    assert r.trigger.seq == 250  # actual latest seq, not the paged 201
    # Bounded payload contract still holds: returned summary has at most
    # 200 events in its window, and last_n is the default 5.
    assert r.summary is not None
    assert r.summary.total_count <= 200
    assert len(r.summary.last_n) == 5
    assert r.summary.next_seq == 250


@pytest.mark.anyio
async def test_watch_next_event_past_summary_page(fake_workspace):
    """``next_event`` trigger must fire when events exist past the page
    boundary, even if the caller's ``since_seq`` is well below it.

    Same root cause as ``test_watch_phase_change_past_summary_page``:
    without the sliding-window fix, ``snap.next_seq`` pins at
    ``since_seq + 200`` and ``snap.next_seq > since_seq`` happens to
    still fire on the fast path — but the *reported* seq would be wrong
    (201 instead of the true latest 250). This test pins the reconnect
    cursor contract: ``trigger.seq`` and ``summary.next_seq`` must both
    reflect the true latest seq so reconnect skips ahead correctly
    instead of replaying the middle of the stream.
    """
    events = [_ev(i, kind="agent.tool_use", phase="plan")
              for i in range(1, 251)]
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=events,
    )

    r = await orcho_run_watch(
        "20260101_000001", since_seq=0, until="next_event", timeout_s=2,
    )
    assert r.triggered is True
    assert r.trigger.kind == "next_event"
    assert r.trigger.seq == 250
    assert r.summary is not None
    assert r.summary.next_seq == 250


@pytest.mark.anyio
async def test_watch_phase_end_is_phase_change(fake_workspace):
    """current_phase transitioning to None (phase end) is still a phase
    change. Guards the 'no is-not-None guard' fix in _watch_should_trigger."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[
            _ev(1, kind="phase.start", phase="plan"),
            _ev(2, kind="phase.end", phase="plan"),
        ],
    )
    # Baseline at since_seq=1 is "plan". Summary's current_phase is None
    # (plan closed at seq=2). Must trigger phase_change.
    r = await orcho_run_watch(
        "20260101_000001", since_seq=1, until="phase_change", timeout_s=5,
    )
    assert r.triggered is True
    assert r.trigger.kind == "phase_change"
    assert r.trigger.phase is None


@pytest.mark.anyio
async def test_watch_terminal(fake_workspace):
    """until=terminal returns immediately when status is done."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "done", "task": "t"},
        events=[_ev(1), _ev(2, kind="run.end")],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0, until="terminal", timeout_s=5,
    )
    assert r.triggered is True
    assert r.trigger.kind == "terminal"
    assert r.trigger.status == "done"
    # handoff is only populated when trigger.kind == "handoff".
    assert r.handoff is None


@pytest.mark.anyio
async def test_watch_summary_false_omits_summary(fake_workspace):
    """summary=False returns summary=None; trigger.seq carries the
    reconnect cursor."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[_ev(1), _ev(2)],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0, until="next_event",
        timeout_s=5, summary=False,
    )
    assert r.triggered is True
    assert r.summary is None
    assert r.trigger.seq == 2  # reconnect cursor still present


@pytest.mark.anyio
async def test_watch_input_validation(fake_workspace):
    """All out-of-range inputs raise InvalidPlanError. Ceiling itself
    is accepted as long as the tool returns via fast-path (no actual
    sleep of the ceiling duration)."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[_ev(1), _ev(2)],
    )

    with pytest.raises(InvalidPlanError):
        await orcho_run_watch(
            "20260101_000001", since_seq=-1, timeout_s=5,
        )
    for bad in (0, -1, _WATCH_TIMEOUT_CEILING + 1):
        with pytest.raises(InvalidPlanError):
            await orcho_run_watch(
                "20260101_000001", since_seq=0, timeout_s=bad,
            )
    with pytest.raises(InvalidPlanError):
        await orcho_run_watch(
            "20260101_000001", since_seq=0,
            until="not_a_real_choice", timeout_s=5,  # type: ignore[arg-type]
        )

    # Valid: at the ceiling, fast-path triggers, no actual long wait.
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0,
        until="next_event", timeout_s=_WATCH_TIMEOUT_CEILING,
    )
    assert r.triggered is True


# ── workspace state wiring (caused by watch reads) ──────────────────────────

@pytest.mark.anyio
async def test_watch_summary_false_still_updates_workspace_state(
    fake_workspace,
):
    """``orcho_run_watch(..., summary=False)`` must still advance the
    advisory state — the snapshot happens inside the watch loop
    regardless of whether the caller wants the summary on the wire."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[_ev(i) for i in range(1, 4)],
    )
    r = await orcho_run_watch(
        "20260101_000001", since_seq=0,
        until="next_event", timeout_s=5, summary=False,
    )
    assert r.summary is None
    assert r.triggered is True

    state = orcho_workspace_state()
    assert "20260101_000001" in state.runs
    assert state.runs["20260101_000001"].last_seq == 3


# ── until="subtask" — step-by-step subtask_dag progress ─────────────────────


@pytest.mark.anyio
async def test_watch_subtask_trigger_fires_on_boundary(fake_workspace):
    """until=subtask returns on a subtask boundary, with the live coordinate
    on summary.current_subtask."""
    write_run(
        fake_workspace, "20260101_000020",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[
            _ev(1, kind="phase.start", phase="implement"),
            _ev(2, kind="subtask.start", phase="implement",
                subtask_id="t1", index=1, total=3, goal="lock scope"),
        ],
    )
    r = await orcho_run_watch(
        "20260101_000020", since_seq=0, until="subtask", timeout_s=5,
    )
    assert r.triggered is True
    assert r.trigger.kind == "subtask"
    assert "1/3" in r.trigger.reason
    assert r.summary is not None
    assert r.summary.current_subtask is not None
    assert r.summary.current_subtask.index == 1
    assert r.summary.current_subtask.total == 3
    assert r.summary.current_subtask.goal == "lock scope"


@pytest.mark.anyio
async def test_watch_subtask_does_not_fire_without_new_boundary(fake_workspace):
    """A subtask boundary already at/below since_seq must not re-fire; the
    watch times out instead of returning a stale subtask trigger."""
    write_run(
        fake_workspace, "20260101_000021",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[
            _ev(1, kind="phase.start", phase="implement"),
            _ev(2, kind="subtask.start", phase="implement",
                subtask_id="t1", index=1, total=3, goal="g"),
        ],
    )
    # Caller already saw seq 2 (the subtask.start); nothing new past it.
    r = await orcho_run_watch(
        "20260101_000021", since_seq=2, until="subtask", timeout_s=1,
    )
    assert r.triggered is False
    assert r.trigger.kind == "timeout"


@pytest.mark.anyio
async def test_watch_subtask_terminal_overrides(fake_workspace):
    """until=subtask still ends with the run: a terminal status returns a
    terminal trigger even with no fresh subtask boundary."""
    write_run(
        fake_workspace, "20260101_000022",
        meta={"project": "/p/x", "status": "done", "task": "t"},
        events=[
            _ev(1, kind="phase.start", phase="implement"),
            _ev(2, kind="subtask.start", phase="implement",
                subtask_id="t1", index=1, total=1, goal="g"),
            _ev(3, kind="subtask.end", phase="implement",
                subtask_id="t1", index=1, total=1, goal="g", ok=True),
            _ev(4, kind="phase.end", phase="implement"),
            _ev(5, kind="run.end", phase=None),
        ],
    )
    r = await orcho_run_watch(
        "20260101_000022", since_seq=5, until="subtask", timeout_s=2,
    )
    assert r.trigger.kind == "terminal"


def test_progress_message_renders_subtask_step():
    """The progress notification text surfaces N/M (goal) for a running
    subtask_dag implement phase."""
    from orcho_mcp.observe.watch import _progress_message
    from orcho_mcp.schemas import CurrentSubtaskRecord, RunEventsSummary

    snap = RunEventsSummary(
        run_id="r", total_count=1, next_seq=7, eof=True, status="running",
        current_phase="implement",
        current_subtask=CurrentSubtaskRecord(
            subtask_id="t3", index=3, total=12, goal="Patch target",
            state="done", seq=7,
        ),
    )
    msg = _progress_message(snap)
    assert "implement" in msg
    assert "subtask 3/12" in msg
    assert "done" in msg
    assert "Patch target" in msg


# ── already-paused handoff wakes immediately, cursor-independent ─────────────
#
# Regression for the captain-recovery contract: a run that is ALREADY paused
# at a phase handoff when the watch is issued must wake the watch immediately
# — the fast-path keys off ``status``, not off a fresh event past the cursor.
# This is what lets an operator reattach to a stranded paused run with
# ``since_seq`` equal to the latest seq (i.e. "I've already seen everything")
# and still get the handoff packet back, instead of waiting out ``timeout_s``.


def _paused_handoff_meta():
    """meta for a run stranded at an ``awaiting_phase_handoff`` pause, with a
    fully-populated ``phase_handoff`` block (id/phase/available_actions/verdict).
    """
    return {
        "project": "/p/x",
        "status": "awaiting_phase_handoff",
        "task": "t",
        "phase_handoff": {
            "id": "validate_plan:plan_round:2",
            "phase": "validate_plan",
            "trigger": "rejected",
            "verdict": "REJECTED",
            "available_actions": ["continue", "retry_feedback", "halt"],
        },
    }


@pytest.mark.anyio
async def test_watch_already_paused_handoff_wakes_at_latest_seq(fake_workspace):
    """An already-paused handoff wakes the watch immediately even when
    ``since_seq`` equals the current latest seq.

    Proves the operator-recovery path: reattaching to a stranded paused run
    with the cursor at "I've seen everything" still returns triggered=True,
    a handoff trigger, the handoff packet (handoff_id/available_actions/
    default_action) and ``summary.pending_handoff`` mirroring meta — all
    without a fresh event past the cursor and without burning ``timeout_s``.
    """
    write_run(
        fake_workspace, "20260101_000050",
        meta=_paused_handoff_meta(),
        events=[_ev(1), _ev(2), _ev(3)],
    )
    # since_seq = latest seq (3): the caller has already observed every event.
    r = await orcho_run_watch(
        "20260101_000050", since_seq=3,
        until="handoff_or_terminal", timeout_s=2,
    )

    assert r.triggered is True
    assert r.trigger.kind == "handoff"

    # Handoff packet present and populated from meta.phase_handoff.
    assert r.handoff is not None
    assert r.handoff.handoff_id == "validate_plan:plan_round:2"
    assert r.handoff.available_actions == ["continue", "retry_feedback", "halt"]
    assert r.handoff.default_action in r.handoff.available_actions

    # summary.pending_handoff mirrors meta — captain sees the decision surface
    # straight from the bounded summary.
    assert r.summary is not None
    ph = r.summary.pending_handoff
    assert ph is not None
    assert ph.handoff_id == "validate_plan:plan_round:2"
    assert ph.phase == "validate_plan"


@pytest.mark.anyio
@pytest.mark.parametrize("since_seq", [0, 3], ids=["cursor-zero", "cursor-latest"])
async def test_watch_already_paused_handoff_cursor_independent(
    fake_workspace, since_seq: int,
):
    """The already-paused handoff trigger is independent of ``since_seq``.

    Both an at-the-start cursor (``since_seq=0``) and an at-the-latest cursor
    (``since_seq=3``, equal to the run's latest seq) wake immediately with the
    same handoff trigger, packet, and ``summary.pending_handoff`` — the
    fast-path is driven by ``status``, not by the cursor position.
    """
    write_run(
        fake_workspace, "20260101_000051",
        meta=_paused_handoff_meta(),
        events=[_ev(1), _ev(2), _ev(3)],
    )
    r = await orcho_run_watch(
        "20260101_000051", since_seq=since_seq,
        until="handoff_or_terminal", timeout_s=2,
    )

    assert r.triggered is True
    assert r.trigger.kind == "handoff"
    assert r.handoff is not None
    assert r.handoff.handoff_id == "validate_plan:plan_round:2"
    assert r.summary is not None
    assert r.summary.pending_handoff is not None
    assert r.summary.pending_handoff.handoff_id == "validate_plan:plan_round:2"
    assert r.summary.pending_handoff.phase == "validate_plan"


# ── reconnect-loop guidance (schema descriptions + tool docstrings) ──────────


def test_reconnect_cursor_fields_document_events_summary_fallback():
    """``WatchTrigger.seq`` and ``RunEventsSummary.next_seq`` must name
    themselves as the reconnect cursor and point at the
    ``orcho_run_events_summary`` fallback — the schema is where the
    resilient observation loop is pinned for typed clients."""
    from orcho_mcp.schemas import RunEventsSummary, WatchTrigger

    seq_desc = (WatchTrigger.model_fields["seq"].description or "").lower()
    assert "reconnect" in seq_desc
    assert "orcho_run_events_summary" in seq_desc

    next_seq_desc = (
        RunEventsSummary.model_fields["next_seq"].description or ""
    ).lower()
    assert "reconnect" in next_seq_desc


def test_watch_docstrings_frame_disconnect_as_observer_loss():
    """The watch / summary tool docstrings recommend a bounded watch,
    name the events_summary fallback for reconnect, and never call a
    client-side disconnect a failed run."""
    watch_doc = (orcho_run_watch.__doc__ or "").lower()
    assert "orcho_run_events_summary" in watch_doc
    assert "reconnect" in watch_doc
    assert "not a failed run" in watch_doc or "not a run failure" in watch_doc

    from orcho_mcp.tools import orcho_run_events_summary

    summary_doc = (orcho_run_events_summary.__doc__ or "").lower()
    assert "reconnect" in summary_doc or "fallback" in summary_doc
    assert "since_seq" in summary_doc
