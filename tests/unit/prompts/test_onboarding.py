"""L1 unit tests for the first-contact onboarding surface.

Pins:

* Both surfaces (prompt + resource) read the same canonical markdown,
  so they cannot drift.
* The content covers every step a first-time user must walk through:
  workspace, profile, start, status, evidence, plan_gate_decide, resume,
  metrics, history, verify-in-project.
* The content stays user-facing — no contributor-only vocabulary about
  planning tags, architecture notes, schema snapshots, banned tokens,
  or internal package layering bleeds into what a user reads.
"""
from __future__ import annotations

import re

import pytest

from orcho_mcp.onboarding import (
    _GETTING_STARTED_PATH,
    getting_started_resource,
    orcho_getting_started,
)

# ── content lives at a known package-relative path ──────────────────────────


def test_canonical_markdown_exists():
    assert _GETTING_STARTED_PATH.is_file(), (
        f"onboarding markdown missing at {_GETTING_STARTED_PATH}"
    )


def test_prompt_and_resource_return_identical_content():
    """Single source of truth — DRY between prompt and resource."""
    prompt_text = orcho_getting_started()
    resource_text = getting_started_resource()
    assert prompt_text == resource_text
    assert prompt_text == _GETTING_STARTED_PATH.read_text(encoding="utf-8")


# ── content covers the full user-facing path ────────────────────────────────


@pytest.mark.parametrize(
    "needle",
    [
        # One-time bootstrap that lays the workspace rails.
        "orcho workspace init",
        # Discovery layer.
        "orcho_workspace_info",
        "orcho_profiles_list",
        # Semantic profile vocabulary the user must distinguish.
        "small_task",
        "feature",
        "planning",
        # Mock vs real-provider semantics.
        "mock=True",
        "mock=False",
        # Lifecycle tools.
        "orcho_run_start",
        "orcho_run_status",
        "awaiting_phase_handoff",
        # Handoff flow.
        "orcho_run_evidence",
        "orcho_phase_handoff_decide",
        "orcho_run_resume",
        # Final inspection.
        "orcho_run_metrics",
        "orcho_run_history",
        # Verification step in the user's own tree.
        "test command",
        "diff",
        # Disposable-copy guidance.
        "disposable",
    ],
)
def test_content_mentions_required_step(needle: str):
    text = orcho_getting_started()
    assert needle in text, f"onboarding text is missing reference to {needle!r}"


# ── content stays user-facing ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "forbidden",
    [
        "RE" + "A-",           # internal planning tags
        "AD" + "R ",           # architecture decision records
        "schema snapshot",     # contributor concept
        "banned token",        # contributor concept
        "open-core",           # contributor concept
        "DEMO-1",              # internal proof-gate naming
        "DEMO-2",
    ],
)
def test_content_avoids_contributor_vocabulary(forbidden: str):
    text = orcho_getting_started()
    assert forbidden.lower() not in text.lower(), (
        f"contributor vocabulary leaked into user onboarding: {forbidden!r}"
    )


# ── handler resilience ──────────────────────────────────────────────────────


def test_prompt_handler_returns_inline_message_when_file_missing(
    monkeypatch, tmp_path,
):
    """A missing markdown file must not crash the server — surface a
    clear inline message so the client UI shows what's wrong."""
    missing_path = tmp_path / "not-here.md"
    monkeypatch.setattr("orcho_mcp.onboarding._GETTING_STARTED_PATH", missing_path)
    text = orcho_getting_started()
    assert "[orcho]" in text
    assert "missing" in text.lower()


# ── shape of the markdown — quick sanity ────────────────────────────────────


def test_content_starts_with_user_friendly_heading():
    text = orcho_getting_started()
    first_line = text.lstrip().splitlines()[0]
    assert first_line.startswith("#"), "content should be a markdown document"
    assert re.search(r"orcho", first_line, re.IGNORECASE), (
        "first heading should name the product"
    )


def _section(text: str, heading: str) -> str:
    """Return one level-two onboarding section, excluding the next one."""
    match = re.search(
        rf"^## {re.escape(heading)}$([\s\S]*?)(?=^## |\Z)",
        text,
        flags=re.MULTILINE,
    )
    assert match, f"onboarding section missing: {heading!r}"
    return match.group(1)


def test_watch_progress_routes_single_shot_and_long_poll_intents() -> None:
    """The onboarding progress section must not steer position checks to status."""
    watch_progress = _section(orcho_getting_started(), "5. Watch progress")

    assert "orcho_run_live_status(run_id=\"<run_id>\")" in watch_progress
    assert "orcho_run_watch(run_id=\"<run_id>\")" in watch_progress
    assert "single-shot progress snapshot" in watch_progress
    assert "long-poll instead" in watch_progress
    assert re.search(r"not to check progress\s+position", watch_progress)


def test_quick_reference_routes_progress_to_live_status_not_status() -> None:
    """Quick-reference tool selection keeps progress and lifecycle separate."""
    quick_reference = _section(orcho_getting_started(), "Quick reference")

    assert "| Check progress | `orcho_run_live_status` |" in quick_reference
    assert "orcho_run_status" not in quick_reference
