"""Full MCP initialize handshake via stdio.

Spawns ``orcho-mcp`` as a real subprocess (the way Claude Code would),
runs the canonical initialize/initialized exchange, and verifies the
server reports zero tools / zero resources / zero prompts. Catches
regressions in stdio framing, capability negotiation, and the FastMCP
loop wiring that the unit-level smoke can't see.

Skipped if ``mcp.client`` SDK isn't available (rare; same package
provides both server and client).
"""
from __future__ import annotations

import pytest

pytest.importorskip("mcp.client.stdio")

from tests.fixtures.stdio import initialized_stdio_session  # noqa: E402


@pytest.mark.anyio
async def test_initialize_returns_empty_catalog():
    async with initialized_stdio_session() as (session, init_result):
        # Server identifies itself as ``orcho``.
        assert init_result.serverInfo.name == "orcho"
        assert init_result.capabilities.resources is not None
        assert init_result.capabilities.resources.subscribe is True
        assert init_result.capabilities.resources.listChanged is True

        # Read-only tools should be registered and listed.
        tools = await session.list_tools()
        tool_names = {t.name for t in tools.tools}
        expected = {
            "orcho_workspace_info",
            "orcho_workspace_state",
            "orcho_run_history",
            "orcho_run_status",
            "orcho_run_metrics",
            "orcho_run_events_tail",
            "orcho_run_events_summary",
            "orcho_run_watch",
            "orcho_plan_validate",
            "orcho_skills_list",
            "orcho_prompts_resolve",
            "orcho_profiles_list",
            "orcho_run_diagnose",
            "orcho_delivery_gate",
        }
        assert expected <= tool_names, f"missing tools: {expected - tool_names}"

        # Resources catalogue includes static URIs; templated ones
        # (with {placeholders}) live in list_resource_templates instead.
        resources = await session.list_resources()
        resource_uris = {str(r.uri) for r in resources.resources}
        assert "orcho://workspace" in resource_uris
        assert "orcho://runs" in resource_uris
        assert "orcho://profiles" in resource_uris

        templates = await session.list_resource_templates()
        template_uris = {str(t.uriTemplate) for t in templates.resourceTemplates}
        assert "orcho://runs/{run_id}/meta" in template_uris
        assert "orcho://runs/{run_id}/metrics" in template_uris
        assert "orcho://runs/{run_id}/events" in template_uris
        assert "orcho://runs/{run_id}/summary" in template_uris
        assert "orcho://runs/{run_id}/parsed_plan.json" in template_uris
        assert "orcho://runs/{run_id}/evidence" in template_uris
        assert "orcho://runs/{run_id}/diff.patch" in template_uris
        assert "orcho://profiles/{name}" in template_uris
        assert "orcho://projects/{project_b64}/skills" in template_uris

        # Prompts catalogue mirrors _prompts/*.md.
        prompts = await session.list_prompts()
        prompt_names = {p.name for p in prompts.prompts}
        # Don't pin the exact set — _prompts/ evolves. Just verify the
        # canonical implement prompt is registered.
        assert "tasks/implement" in prompt_names


@pytest.fixture
def anyio_backend():
    # Pin to asyncio — pytest-anyio defaults try trio too, which we don't ship.
    return "asyncio"
