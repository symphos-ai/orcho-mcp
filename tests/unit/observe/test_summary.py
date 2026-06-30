"""Unit tests for ``orcho_run_events_summary``.

Covers bounded summary behavior, event compaction, next-action
derivation from merged status, and the workspace-state observation
side effect that fires on every summary read.
"""
from __future__ import annotations

import pytest

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.tools import (
    orcho_run_events_summary,
    orcho_workspace_state,
)
from tests.fixtures.mcp_workspace import meta, write_run


def _ev(seq: int, kind: str = "phase.start", phase: str = "plan", **payload):
    return {"seq": seq, "ts": f"2026-01-01T00:00:{seq:02d}", "kind": kind,
            "phase": phase, "payload": payload}


# ── orcho_run_events_summary ────────────────────────────────────────────────

def test_events_summary_basic_counts(fake_workspace):
    """5 events / 2 phases / 3 kinds — aggregate matches the fixture."""
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="running", project="/p/x", task="t"),
        events=[
            _ev(1, kind="phase.start", phase="plan"),
            _ev(2, kind="agent.text", phase="plan"),
            _ev(3, kind="phase.end", phase="plan"),
            _ev(4, kind="phase.start", phase="implement"),
            _ev(5, kind="agent.tool_use", phase="implement"),
        ],
    )

    r = orcho_run_events_summary("20260101_000001")
    assert r.run_id == "20260101_000001"
    assert r.total_count == 5
    assert r.next_seq == 5
    assert r.eof is True
    # by_kind: 3 distinct kinds.
    assert r.by_kind == {
        "phase.start": 2,
        "agent.text": 1,
        "phase.end": 1,
        "agent.tool_use": 1,
    }
    # by_phase: first-seen order is plan, implement.
    assert [p.phase for p in r.by_phase] == ["plan", "implement"]
    plan_bucket = next(p for p in r.by_phase if p.phase == "plan")
    impl_bucket = next(p for p in r.by_phase if p.phase == "implement")
    assert plan_bucket.count == 3
    assert sorted(plan_bucket.kinds) == ["agent.text", "phase.end", "phase.start"]
    assert impl_bucket.count == 2
    assert sorted(impl_bucket.kinds) == ["agent.tool_use", "phase.start"]
    # current_phase: implement started, plan ended earlier — implement wins.
    assert r.current_phase == "implement"
    # last_n: 5 most-recent (capped by total count).
    assert [e.seq for e in r.last_n] == [1, 2, 3, 4, 5]


def test_events_summary_bounded_payload(fake_workspace):
    """220 events with a 10 KB oversized payload each → serialised ≤ 10 KB."""

    big_command = "x" * 10_000  # 10 KB of garbage per event payload
    events = [
        {
            "seq": i,
            "ts": f"2026-01-01T00:00:{i:02d}",
            "kind": "agent.tool_use" if i % 3 else "phase.start",
            "phase": "implement",
            "payload": {
                # The raw payload should be DROPPED by the compact projection.
                "command": big_command,
                # These small fields should survive (truncated).
                "tool": "Bash",
                "summary": "ran a command",
            },
        }
        for i in range(1, 221)
    ]
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=events,
    )

    r = orcho_run_events_summary("20260101_000001", limit=200)
    # Window honoured: 200 events considered (not the full 220, not the
    # 1000 ceiling).
    assert r.total_count == 200
    # Compact events MUST NOT carry the raw 10 KB command field.
    for ce in r.last_n:
        # CompactRunEvent has no `command` attribute at all — payload
        # was dropped at projection time. Sanity-check by serialising.
        ce_json = ce.model_dump_json()
        assert big_command not in ce_json
        assert len(ce_json) < 1_000  # well under 1 KB per compact event
    # Whole-summary wire size budget.
    wire = r.model_dump_json()
    assert big_command not in wire
    assert len(wire.encode("utf-8")) <= 10_000, (
        f"summary wire size {len(wire.encode('utf-8'))} exceeded 10 KB"
    )


def test_events_summary_since_seq_and_limit(fake_workspace):
    """since_seq=20, limit=10 → window covers events 21..30, eof=False."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[_ev(i, kind="agent.text", phase="implement") for i in range(1, 51)],
    )

    r = orcho_run_events_summary("20260101_000001", since_seq=20, limit=10)
    assert r.total_count == 10
    assert r.next_seq == 30
    assert r.eof is False  # 20 more events exist past next_seq
    # last_n defaults to 5 — picked from the windowed tail.
    assert [e.seq for e in r.last_n] == [26, 27, 28, 29, 30]


def test_events_summary_default_limit_is_small(fake_workspace):
    """Default status reads consider 50 events; callers can opt into more."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[_ev(i, kind="agent.text", phase="implement") for i in range(1, 81)],
    )

    r = orcho_run_events_summary("20260101_000001")
    assert r.total_count == 50
    assert r.next_seq == 50
    assert r.eof is False
    assert [e.seq for e in r.last_n] == [46, 47, 48, 49, 50]


def test_events_summary_current_phase_outside_window(fake_workspace):
    """Regression: current_phase must be tracked over the FULL event
    stream up to next_seq, not just the windowed slice. Without this,
    a poll that lands inside an active phase loses the phase context.
    """
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[
            _ev(1, kind="phase.start", phase="implement"),
            _ev(2, kind="agent.tool_use", phase="implement"),
            _ev(3, kind="agent.tool_use", phase="implement"),
            _ev(4, kind="agent.tool_use", phase="implement"),
            _ev(5, kind="agent.tool_use", phase="implement"),
        ],
    )

    # Window only sees seq 2..5; phase.start@1 is OUTSIDE the window.
    r = orcho_run_events_summary("20260101_000001", since_seq=1)
    assert r.current_phase == "implement", (
        "current_phase must be computed from the full event stream up to "
        "next_seq, not just the windowed slice"
    )


def test_events_summary_current_phase_tracking(fake_workspace):
    """phase.start(plan) → phase.end(plan) → phase.start(implement) =>
    current_phase == 'implement' (plan is closed)."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[
            _ev(1, kind="phase.start", phase="plan"),
            _ev(2, kind="phase.end", phase="plan"),
            _ev(3, kind="phase.start", phase="implement"),
        ],
    )

    r = orcho_run_events_summary("20260101_000001")
    assert r.current_phase == "implement"


def test_events_summary_next_actions(fake_workspace):
    """awaiting_phase_handoff → next_actions includes the handoff-decide step."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "awaiting_phase_handoff", "task": "t"},
        events=[_ev(1, kind="phase.start", phase="validate_plan")],
    )

    r = orcho_run_events_summary("20260101_000001")
    assert r.status == "awaiting_phase_handoff"
    assert any("orcho_phase_handoff_decide" in a for a in r.next_actions)


@pytest.mark.parametrize(
    "status",
    ["running", "done", "failed", "halted", "interrupted", "awaiting_phase_handoff"],
)
def test_events_summary_next_actions_status_deterministic(fake_workspace, status: str):
    """``next_actions`` is derived from ``status`` alone — same status,
    same actions, regardless of the event stream content.

    Pins the "no invention" contract: two runs with identical ``meta.status``
    must produce identical ``next_actions``, so an operator-side LLM can
    cache or branch on the action set without worrying about event-stream
    drift influencing the suggestions.
    """
    # Two separate runs with the same status but different event streams.
    write_run(
        fake_workspace, "run_a",
        meta={"project": "/p/x", "status": status, "task": "task A"},
        events=[_ev(1, kind="phase.start", phase="plan")],
    )
    write_run(
        fake_workspace, "run_b",
        meta={"project": "/p/y", "status": status, "task": "task B"},
        events=[
            _ev(1, kind="phase.start", phase="implement"),
            _ev(2, kind="agent.text", phase="implement"),
            _ev(3, kind="phase.end", phase="implement"),
        ],
    )

    r_a = orcho_run_events_summary("run_a")
    r_b = orcho_run_events_summary("run_b")
    assert r_a.next_actions == r_b.next_actions, (
        f"next_actions diverged for status={status!r} despite identical "
        f"meta.status. The contract is state-derived, not event-derived.\n"
        f"  run_a: {r_a.next_actions}\n"
        f"  run_b: {r_b.next_actions}"
    )


def test_events_summary_last_n_ordering_edge(fake_workspace):
    """``last_n`` returns the K most-recent events from the window IN
    SEQ ORDER (ascending), not reversed — even when K < total.

    Pins the slice direction. ``windowed[-K:]`` is the natural Python
    pattern; a hand-rolled implementation could accidentally reverse
    when sorting "by recency". This test fails fast on the reverse.
    """
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[_ev(i) for i in range(1, 11)],  # seqs 1..10
    )

    r = orcho_run_events_summary("20260101_000001", last_n=3)
    # 3 most-recent: seqs 8, 9, 10 (ascending). NOT [10, 9, 8].
    assert [e.seq for e in r.last_n] == [8, 9, 10]


def test_events_summary_input_validation(fake_workspace):
    """All out-of-range inputs raise InvalidPlanError. Edge cases inside
    the ceiling succeed, and last_n=0 returns an empty list (guard
    against the ``windowed[-0:]`` whole-list gotcha)."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p/x", "status": "running", "task": "t"},
        events=[_ev(i) for i in range(1, 11)],
    )

    # Rejected: limit out of range.
    for bad_limit in (0, -1, 1001):
        with pytest.raises(InvalidPlanError):
            orcho_run_events_summary("20260101_000001", limit=bad_limit)

    # Rejected: last_n out of range.
    for bad_last_n in (-1, 101):
        with pytest.raises(InvalidPlanError):
            orcho_run_events_summary("20260101_000001", last_n=bad_last_n)

    # Rejected: negative since_seq.
    with pytest.raises(InvalidPlanError):
        orcho_run_events_summary("20260101_000001", since_seq=-1)

    # Valid edge: limit=1, last_n=0 → last_n must be empty list, NOT the
    # whole window (Python's ``windowed[-0:]`` returns the whole list).
    r = orcho_run_events_summary("20260101_000001", limit=1, last_n=0)
    assert r.last_n == []
    assert r.total_count == 1  # window respects limit=1

    # Valid edge: at the ceiling.
    r = orcho_run_events_summary(
        "20260101_000001", limit=1000, last_n=100,
    )
    assert r.total_count == 10  # only 10 events exist; window is bounded by data


# ── workspace state wiring (caused by summary reads) ────────────────────────

def test_events_summary_updates_workspace_state(fake_workspace):
    """Every call to ``orcho_run_events_summary`` records an
    observation in the advisory state file. The tool reads it back via
    ``orcho_workspace_state`` with the bounded wire shape."""
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p/x",
            "status": "running",
            "task": "state wiring",
        },
        events=[_ev(i) for i in range(1, 6)],
    )
    r = orcho_run_events_summary("20260101_000001")
    assert r.next_seq == 5

    state = orcho_workspace_state()
    assert state.version == 1
    assert "20260101_000001" in state.runs
    record = state.runs["20260101_000001"]
    assert record.last_seq == 5
    assert record.last_status == "running"
    # The default ``_ev`` shape emits ``phase.start`` events on phase
    # ``plan``; that's the open phase at the cursor.
    assert record.last_phase == "plan"
    assert record.last_summary_at  # non-empty timestamp


# ── current_subtask projection (live subtask_dag progress) ──────────────────


def test_current_subtask_tracks_latest_boundary(fake_workspace):
    """current_subtask reflects the latest subtask.start/end with its
    index/total/goal/state coordinate."""
    write_run(
        fake_workspace, "20260101_000010",
        meta=meta(status="running", project="/p/x", task="t"),
        events=[
            _ev(1, kind="phase.start", phase="implement"),
            _ev(2, kind="subtask.start", phase="implement",
                subtask_id="t1", index=1, total=3, goal="lock scope"),
            _ev(3, kind="subtask.end", phase="implement",
                subtask_id="t1", index=1, total=3, goal="lock scope", ok=True),
            _ev(4, kind="subtask.start", phase="implement",
                subtask_id="t2", index=2, total=3, goal="apply fix"),
        ],
    )
    r = orcho_run_events_summary("20260101_000010")
    cs = r.current_subtask
    assert cs is not None
    assert cs.subtask_id == "t2"
    assert cs.index == 2
    assert cs.total == 3
    assert cs.goal == "apply fix"
    assert cs.state == "running"
    assert cs.seq == 4


def test_current_subtask_end_state_incomplete(fake_workspace):
    """A subtask.end carrying attestation_error maps to state=incomplete."""
    write_run(
        fake_workspace, "20260101_000011",
        meta=meta(status="running", project="/p/x", task="t"),
        events=[
            _ev(1, kind="phase.start", phase="implement"),
            _ev(2, kind="subtask.start", phase="implement",
                subtask_id="t1", index=1, total=1, goal="g"),
            _ev(3, kind="subtask.end", phase="implement",
                subtask_id="t1", index=1, total=1, goal="g",
                ok=True, attestation_error="done_criteria not met (by index): [1]"),
        ],
    )
    r = orcho_run_events_summary("20260101_000011")
    assert r.current_subtask is not None
    assert r.current_subtask.state == "incomplete"
    assert r.current_subtask.index == 1


def test_current_subtask_cleared_when_implement_ends(fake_workspace):
    """phase.end clears current_subtask so a stale coordinate does not leak
    into review/final_acceptance."""
    write_run(
        fake_workspace, "20260101_000012",
        meta=meta(status="running", project="/p/x", task="t"),
        events=[
            _ev(1, kind="phase.start", phase="implement"),
            _ev(2, kind="subtask.start", phase="implement",
                subtask_id="t1", index=1, total=1, goal="g"),
            _ev(3, kind="subtask.end", phase="implement",
                subtask_id="t1", index=1, total=1, goal="g", ok=True),
            _ev(4, kind="phase.end", phase="implement"),
            _ev(5, kind="phase.start", phase="review_changes"),
        ],
    )
    r = orcho_run_events_summary("20260101_000012")
    assert r.current_subtask is None
    assert r.current_phase == "review_changes"
