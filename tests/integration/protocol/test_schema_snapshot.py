"""Schema snapshot — golden-file diff for the public MCP catalog.

L1+L2 layer in the methodology: drives the same FastMCP introspection
methods Claude Code calls over the wire, compares against the committed
``docs/mcp_schema.json``. Catches schema drift (anti-pattern #5):
Pydantic models change → JSON Schema changes → clients see a different
shape, and without this test nothing fails until a downstream consumer
breaks in production.

Failure path:
  1. Test fails with a diff showing what changed.
  2. Reviewer decides whether the change is intentional.
  3. If yes: ``python tools/dump_mcp_schema.py`` regenerates the file,
     it goes into the same commit as the implementation change.
  4. If no: revert the implementation.

This is the closest thing MCP has to OpenAPI breaking-change linting.
"""
from __future__ import annotations

import json
from pathlib import Path

from orcho_mcp.discovery import collect_catalog

_REPO_ROOT = Path(__file__).parent.parent.parent.parent.resolve()
_SCHEMA_FILE = _REPO_ROOT / "docs" / "mcp_schema.json"


def _load_committed_schema() -> dict:
    assert _SCHEMA_FILE.is_file(), (
        f"{_SCHEMA_FILE} missing — generate it with "
        "`python tools/dump_mcp_schema.py`."
    )
    return json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))


def test_schema_matches_committed_snapshot():
    """The live catalog matches docs/mcp_schema.json byte-for-byte (logically).

    Compare parsed structures rather than raw text so whitespace
    differences don't trip the test — only structural changes count.
    """
    live = collect_catalog()
    committed = _load_committed_schema()

    if live != committed:
        # Print enough context for the failure message to be actionable.
        live_tools = {t["name"] for t in live["tools"]}
        committed_tools = {t["name"] for t in committed["tools"]}
        live_resources = {r["uri"] for r in live["resources"]}
        committed_resources = {r["uri"] for r in committed["resources"]}
        live_templates = {t["uriTemplate"] for t in live["resourceTemplates"]}
        committed_templates = {
            t["uriTemplate"] for t in committed["resourceTemplates"]
        }
        live_prompts = {p["name"] for p in live["prompts"]}
        committed_prompts = {p["name"] for p in committed["prompts"]}

        diff_lines = []
        for label, live_set, committed_set in (
            ("tools", live_tools, committed_tools),
            ("resources", live_resources, committed_resources),
            ("templates", live_templates, committed_templates),
            ("prompts", live_prompts, committed_prompts),
        ):
            added = live_set - committed_set
            removed = committed_set - live_set
            if added or removed:
                diff_lines.append(f"  {label}:")
                if added:
                    diff_lines.append(f"    + {sorted(added)}")
                if removed:
                    diff_lines.append(f"    - {sorted(removed)}")

        if not diff_lines:
            diff_lines.append(
                "  (set membership identical — schema bodies differ; "
                "regenerate to see the change)"
            )

        raise AssertionError(
            "MCP catalog drift detected — docs/mcp_schema.json is stale.\n"
            "Surface changes:\n"
            + "\n".join(diff_lines)
            + "\n\n"
            + "Run: python tools/dump_mcp_schema.py\n"
            + "If the change is intentional, commit the regenerated file. "
            + "If not, revert the implementation."
        )


def test_committed_snapshot_has_expected_shape():
    """Defensive sanity: file exists, parses, top-level keys are present.

    Cheap canary against accidentally committing an empty / malformed
    snapshot when the dump script breaks silently.
    """
    committed = _load_committed_schema()
    for key in ("tools", "resources", "resourceTemplates", "prompts"):
        assert key in committed, f"missing top-level key: {key}"
        assert isinstance(committed[key], list), f"{key} must be a list"
    # The catalog is non-empty — guard against accidental truncation that
    # would silently pass the structural check above.
    assert len(committed["tools"]) >= 9
    assert len(committed["resources"]) >= 3
    assert len(committed["resourceTemplates"]) >= 5
    assert len(committed["prompts"]) >= 1
    # The focused delivery-gate read-tool is part of the published catalog.
    tool_names = {t["name"] for t in committed["tools"]}
    assert "orcho_delivery_gate" in tool_names
    assert "orcho_delivery_decide" in tool_names


def test_run_inspection_tools_explain_their_operator_questions():
    """The public MCP catalog tells clients which read tool to choose."""
    committed = _load_committed_schema()
    descriptions = {
        tool["name"]: tool.get("description") or ""
        for tool in committed["tools"]
    }

    # run_status is the durable snapshot and routes live-progress questions to
    # orcho_run_live_status rather than positioning itself as the progress view.
    assert (
        "Durable status snapshot for one run"
        in descriptions["orcho_run_status"]
    )
    assert "orcho_run_live_status" in descriptions["orcho_run_status"]
    assert (
        "What happened / what proves it?"
        in descriptions["orcho_run_evidence"]
    )
    assert "How much did it consume?" in descriptions["orcho_run_metrics"]
    assert "What changed?" in descriptions["orcho_run_diff"]
