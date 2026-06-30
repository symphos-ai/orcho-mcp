"""Client interaction profiles for the MCP handoff packet.

Tiny presentation-only adapter: takes an explicit ``interaction_client``
hint and returns a populated ``HandoffClientHints`` for the
``orcho_run_watch`` response. This is metadata, not behaviour — none of
the fields here affect run lifecycle, available actions, decision
correctness, or ``orcho_phase_handoff_decide`` semantics. The agent
consumes these flags to decide *how* to render the prompt; Orcho only
ever returns data.

Design constraints (load-bearing):
- ``interaction_client`` is **explicit**, never detected. No env
  sniffing, no process scanning, no IDE introspection. The operator
  passes it on the watch call or accepts the ``generic`` default.
- Unknown clients normalise to ``generic`` — refusing them at the wire
  would break forward compatibility with future clients we have not
  named yet.
- This module is data-only: no IO, no orcho-core imports, no external
  network or filesystem access. The table is the implementation.

First-class profiles:
- ``generic``      — neutral defaults, chat-style render.
- ``claude-code``  — structured-choice prompt, concise tone.
- ``codex``        — ``ask`` interaction style, concise tone.

Adding a profile is one entry in ``PROFILES`` plus a test. Removing a
profile is a wire-format break.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from orcho_mcp.schemas import HandoffClientHints

InteractionClient = Literal["generic", "claude-code", "codex"]


@dataclass(frozen=True)
class InteractionProfile:
    """Frozen presentation profile for one named client.

    All fields map 1:1 onto ``HandoffClientHints`` (the wire shape) plus
    a ``prompt_tone`` slot used to vary the closing line of the rendered
    handoff prompt. Frozen so a profile cannot be mutated mid-render —
    the table is the source of truth.
    """

    client: str
    interaction_style: str
    preferred_render: str
    show_actions: bool
    allow_feedback_text: bool
    clarify_on_ambiguous_reply: bool
    include_followup_tools: bool
    prompt_tone: str


PROFILES: Final[dict[str, InteractionProfile]] = {
    "generic": InteractionProfile(
        client="generic",
        interaction_style="free_form",
        preferred_render="chat",
        show_actions=True,
        allow_feedback_text=True,
        clarify_on_ambiguous_reply=True,
        include_followup_tools=True,
        prompt_tone="neutral",
    ),
    "claude-code": InteractionProfile(
        client="claude-code",
        interaction_style="structured_choice",
        preferred_render="chat",
        show_actions=True,
        allow_feedback_text=True,
        clarify_on_ambiguous_reply=True,
        include_followup_tools=True,
        prompt_tone="concise",
    ),
    "codex": InteractionProfile(
        client="codex",
        interaction_style="ask",
        preferred_render="ask",
        show_actions=True,
        allow_feedback_text=True,
        clarify_on_ambiguous_reply=True,
        include_followup_tools=True,
        prompt_tone="concise",
    ),
}


def get_interaction_profile(client: str | None) -> InteractionProfile:
    """Resolve a profile by name, defaulting unknown / missing to ``generic``.

    Forward-compatible: any string we have not added to ``PROFILES`` —
    including future clients, typos, and ``None`` — returns the safe
    generic profile. Refusing unknown clients here would break the
    "unknown client => generic" product rule and risk false negatives
    for clients we have not named yet.
    """
    if client is None:
        return PROFILES["generic"]
    return PROFILES.get(client, PROFILES["generic"])


def profile_to_client_hints(profile: InteractionProfile) -> HandoffClientHints:
    """Project a ``InteractionProfile`` onto the wire-format
    ``HandoffClientHints`` model.

    The two shapes are deliberately almost identical so the projection
    is mechanical — ``prompt_tone`` is the only profile-side field that
    does not surface on the wire (it influences ``recommended_user_prompt``
    inside the watch tool instead). ``action_field`` / ``feedback_field``
    stay at their schema defaults because this adapter only varies
    presentation style per-client.
    """
    return HandoffClientHints(
        client=profile.client,
        interaction_style=profile.interaction_style,
        preferred_render=profile.preferred_render,
        show_actions=profile.show_actions,
        allow_feedback_text=profile.allow_feedback_text,
        clarify_on_ambiguous_reply=profile.clarify_on_ambiguous_reply,
        include_followup_tools=profile.include_followup_tools,
    )


__all__ = [
    "InteractionClient",
    "InteractionProfile",
    "PROFILES",
    "get_interaction_profile",
    "profile_to_client_hints",
]
