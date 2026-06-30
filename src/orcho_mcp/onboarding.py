"""orcho_mcp.onboarding — single-source first-contact surface.

Exposes one user-facing getting-started document through two MCP
discovery channels:

* MCP **prompt** ``orcho_getting_started`` — visible in slash menus,
  prompt pickers, and similar UI affordances any MCP-aware client
  surfaces. This is the canonical "start here" entry.
* MCP **resource** ``orcho://docs/getting-started`` — same content,
  addressable by URI for clients that prefer ``resources/list`` /
  ``resources/read`` over prompts.

Both handlers return identical content from
:data:`_GETTING_STARTED_PATH`, so there is no drift between the two
surfaces.

This module deliberately avoids contributor-facing language. The
content is for someone running Orcho against their own project, not
for someone editing Orcho itself.
"""
from __future__ import annotations

from pathlib import Path

from orcho_mcp.instance import mcp

_GETTING_STARTED_PATH: Path = (
    Path(__file__).resolve().parent / "_onboarding" / "getting_started.md"
)


def _read_getting_started() -> str:
    """Read the canonical onboarding markdown.

    File-not-found surfaces as a clear inline message rather than a
    server crash — the prompt/resource still resolves and tells the
    client what's wrong.
    """
    try:
        return _GETTING_STARTED_PATH.read_text(encoding="utf-8")
    except OSError as e:
        return (
            "[orcho] getting-started document is missing on the server. "
            f"Expected at: {_GETTING_STARTED_PATH}. ({e})"
        )


@mcp.prompt(
    name="orcho_getting_started",
    description=(
        "First-contact walkthrough: how to run your first Orcho job through "
        "MCP. Covers workspace check, profile choice, starting a run, "
        "watching progress, the QA gate flow (inspect findings → decide → "
        "resume), inspecting final evidence/metrics/history, and verifying "
        "changes in your project."
    ),
)
def orcho_getting_started() -> str:
    """Return the user-facing getting-started walkthrough."""
    return _read_getting_started()


@mcp.resource(
    "orcho://docs/getting-started",
    name="orcho_getting_started_doc",
    description=(
        "Same content as the orcho_getting_started prompt, addressable as a "
        "resource for clients that prefer URI-based discovery."
    ),
    mime_type="text/markdown",
)
def getting_started_resource() -> str:
    """Return the same onboarding markdown over the resource URI."""
    return _read_getting_started()


__all__ = ["orcho_getting_started", "getting_started_resource"]
