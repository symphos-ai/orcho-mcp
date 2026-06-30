"""Pure projection of ``phase.provider_session_fallback`` events (T5).

``project_provider_session_fallback`` turns a recovered missing-provider-
session event payload into structured fields. These tests pin the fixed
``fresh_provider_session`` mode, the preserved-worktree flag, the
recovered→``phase_succeeded`` mapping, and — critically — the session-id
redaction that never surfaces the full identifier.
"""
from __future__ import annotations

from orcho_mcp.services.run_projection import (
    is_provider_session_fallback_event,
    project_provider_session_fallback,
)


def test_projection_maps_recovered_event():
    p = project_provider_session_fallback({
        "phase": "implement",
        "stale_session_id": "sess_1234567890abcdef",
        "recovered": True,
    })

    assert p.phase == "implement"
    assert p.fallback_mode == "fresh_provider_session"
    assert p.worktree_preserved is True
    assert p.phase_succeeded is True


def test_projection_redacts_full_session_id():
    full = "sess_1234567890abcdef"
    p = project_provider_session_fallback({"stale_session_id": full})

    # The full identifier is never surfaced; a short prefix + ellipsis is.
    assert p.stale_session_id != full
    assert full not in (p.stale_session_id or "")
    assert p.stale_session_id.endswith("…")
    assert p.stale_session_id == "sess_123…"


def test_projection_redacts_short_session_id_without_full_reveal():
    # Even a short id must not be revealed in full.
    p = project_provider_session_fallback({"stale_session_id": "abcd"})
    assert p.stale_session_id != "abcd"
    assert p.stale_session_id.endswith("…")
    assert len(p.stale_session_id) < len("abcd") + 1  # only a partial prefix


def test_projection_passes_through_unknown_sentinel():
    p = project_provider_session_fallback({"stale_session_id": "unknown"})
    assert p.stale_session_id == "unknown"


def test_projection_recovered_absent_defaults_succeeded():
    # core only emits this event after a successful retry; absent recovered
    # still means the phase succeeded.
    p = project_provider_session_fallback({"phase": "implement"})
    assert p.phase_succeeded is True
    assert p.stale_session_id is None


def test_event_kind_predicate():
    assert is_provider_session_fallback_event("phase.provider_session_fallback")
    assert not is_provider_session_fallback_event("phase.end")
