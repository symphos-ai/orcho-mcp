"""Server-level instructions contract.

``instance.py`` composes the shared FastMCP instance with an intent→tool
map in ``instructions`` so clients pick a tool by what they want to do,
not by scanning every tool description. The load-bearing route this guard
protects is the progress / where-is-the-run intent pointing at
``orcho_run_live_status`` (with ``orcho_run_watch`` for the long-poll).
"""
from __future__ import annotations

from orcho_mcp.instance import INSTRUCTIONS, mcp

# Public-boundary split terms that must never appear in client-facing text.
_BANNED_TERMS = (
    "desktop",
    "pywebview",
    "commercial",
    "proprietary",
    "paid",
    "premium",
    "license-gate",
    "enterprise tier",
)


def test_instance_carries_nonempty_instructions() -> None:
    """The FastMCP instance advertises a non-empty instruction string."""
    assert mcp.instructions
    assert mcp.instructions == INSTRUCTIONS


def test_progress_route_points_at_live_status() -> None:
    """The progress / where-is-the-run intent routes to
    ``orcho_run_live_status`` (long-poll via ``orcho_run_watch``)."""
    text = mcp.instructions
    assert "orcho_run_live_status" in text
    assert "orcho_run_watch" in text
    # The route is anchored to the progress / where-is-the-run intent, not
    # just mentioned somewhere unrelated.
    assert "progress" in text.lower()
    assert "where is the run" in text.lower()


def test_instructions_avoid_split_terms() -> None:
    """Client-facing instructions stay on the public boundary."""
    lowered = mcp.instructions.lower()
    hits = [term for term in _BANNED_TERMS if term in lowered]
    assert not hits, f"split terms leaked into server instructions: {hits}"
