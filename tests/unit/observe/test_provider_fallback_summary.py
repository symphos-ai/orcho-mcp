"""Provider-session fallback visibility + non-degradation (T5).

A recovered missing-provider-session fallback is emitted by orcho-core
ONLY after a fresh-session retry already succeeded. These tests pin that
``orcho_run_events_summary`` surfaces it as structured data with a redacted
session id, and — the core guarantee — that its presence never degrades
the run's merged status into a phase failure.
"""
from __future__ import annotations

from orcho_mcp.services.run_projection import merged_status_from_meta
from orcho_mcp.tools import orcho_run_events_summary
from tests.fixtures.mcp_workspace import meta, write_run


def _ev(seq, kind, *, phase=None, payload=None):
    return {"seq": seq, "ts": f"2026-01-01T00:00:{seq:02d}", "kind": kind,
            "phase": phase, "payload": payload or {}}


def _fallback_ev(seq, *, phase="implement", sid="sess_1234567890abcdef"):
    return _ev(
        seq, "phase.provider_session_fallback", phase=phase,
        payload={"phase": phase, "stale_session_id": sid, "recovered": True},
    )


def test_summary_surfaces_provider_session_fallback(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
        events=[
            _ev(1, "phase.start", phase="implement"),
            _fallback_ev(2),
            _ev(3, "phase.end", phase="implement"),
            _ev(4, "run.end"),
        ],
    )

    r = orcho_run_events_summary("20260101_000001")

    assert len(r.provider_session_fallbacks) == 1
    fb = r.provider_session_fallbacks[0]
    assert fb.phase == "implement"
    assert fb.fallback_mode == "fresh_provider_session"
    assert fb.worktree_preserved is True
    assert fb.phase_succeeded is True
    # Redacted, never the full id.
    assert fb.stale_session_id == "sess_123…"
    assert "sess_1234567890abcdef" not in (fb.stale_session_id or "")


def test_successful_fallback_does_not_degrade_status(fake_workspace):
    # A run that recovered from a missing provider session is still done —
    # the fallback event must not flip the merged status to a failure.
    run_dir = write_run(
        fake_workspace, "20260101_000002",
        meta=meta(status="done", project="/p/x", task="t"),
        events=[
            _ev(1, "phase.start", phase="implement"),
            _fallback_ev(2),
            _ev(3, "phase.end", phase="implement"),
            _ev(4, "run.end"),
        ],
    )

    # Direct status-merge: meta + supervisor only; the event stream cannot
    # degrade it.
    assert merged_status_from_meta(
        {"status": "done"}, run_dir,
    ) == "done"

    r = orcho_run_events_summary("20260101_000002")
    assert r.status == "done"
    assert r.status not in ("failed", "interrupted", "halted", "orphaned")
    # The fallback is reported as a recovery, not a failure.
    assert all(fb.phase_succeeded for fb in r.provider_session_fallbacks)


def test_running_with_fallback_stays_running(fake_workspace):
    write_run(
        fake_workspace, "20260101_000003",
        meta=meta(status="running", project="/p/x", task="t"),
        events=[
            _ev(1, "phase.start", phase="implement"),
            _fallback_ev(2),
        ],
    )

    r = orcho_run_events_summary("20260101_000003")
    assert r.status == "running"
    assert len(r.provider_session_fallbacks) == 1


def test_summary_no_fallbacks_when_none(fake_workspace):
    write_run(
        fake_workspace, "20260101_000004",
        meta=meta(status="running", project="/p/x", task="t"),
        events=[_ev(1, "phase.start", phase="plan")],
    )

    r = orcho_run_events_summary("20260101_000004")
    assert r.provider_session_fallbacks == []
