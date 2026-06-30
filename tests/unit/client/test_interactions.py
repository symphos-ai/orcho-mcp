"""L1 unit tests for ``orcho_mcp.client_interactions``.

Pure data-table tests: profile lookup, unknown-client fallback, and the
``InteractionProfile → HandoffClientHints`` projection. No fixtures
involved — the module has no IO surface.

Why a dedicated file: keeps the table-tests away from the L1 watch tests
in ``test_read_tools.py``, which exercise the integration path through
``orcho_run_watch``. Removing or renaming a profile here is a
wire-format change; these tests are the canary.
"""
from __future__ import annotations

from orcho_mcp.client_interactions import (
    PROFILES,
    get_interaction_profile,
    profile_to_client_hints,
)


def test_known_profiles_resolve_to_their_own_client():
    """Each first-class profile name resolves to a profile whose
    ``client`` field matches the lookup key — refuses silent aliasing."""
    assert get_interaction_profile("generic").client == "generic"
    assert get_interaction_profile("claude-code").client == "claude-code"
    assert get_interaction_profile("codex").client == "codex"

    # And the table itself only knows the three first-class clients.
    assert set(PROFILES.keys()) == {"generic", "claude-code", "codex"}


def test_unknown_profile_falls_back_to_generic():
    """Unknown / ``None`` clients use the ``generic`` profile so future
    clients we have not named yet stay forward-compatible."""
    assert get_interaction_profile("cursor").client == "generic"
    assert get_interaction_profile("antigravity").client == "generic"
    assert get_interaction_profile("").client == "generic"
    assert get_interaction_profile(None).client == "generic"


def test_profile_to_client_hints_codex():
    """``codex`` profile projects onto the Ask-style hint set."""
    hints = profile_to_client_hints(get_interaction_profile("codex"))
    assert hints.client == "codex"
    assert hints.interaction_style == "ask"
    assert hints.preferred_render == "ask"
    assert hints.show_actions is True
    assert hints.allow_feedback_text is True
    assert hints.clarify_on_ambiguous_reply is True
    assert hints.include_followup_tools is True
    # Field-name defaults are profile-invariant.
    assert hints.action_field == "action"
    assert hints.feedback_field == "feedback"


def test_profile_to_client_hints_claude_code():
    """``claude-code`` profile projects onto a structured-choice chat
    hint set."""
    hints = profile_to_client_hints(get_interaction_profile("claude-code"))
    assert hints.client == "claude-code"
    assert hints.interaction_style == "structured_choice"
    assert hints.preferred_render == "chat"
    assert hints.show_actions is True
    assert hints.allow_feedback_text is True
    assert hints.clarify_on_ambiguous_reply is True
    assert hints.include_followup_tools is True


def test_profile_to_client_hints_generic():
    """``generic`` profile projects onto the free-form chat hint set."""
    hints = profile_to_client_hints(get_interaction_profile("generic"))
    assert hints.client == "generic"
    assert hints.interaction_style == "free_form"
    assert hints.preferred_render == "chat"
    assert hints.show_actions is True
    assert hints.allow_feedback_text is True
    assert hints.clarify_on_ambiguous_reply is True
    assert hints.include_followup_tools is True
