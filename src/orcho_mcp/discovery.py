"""orcho_mcp.discovery — single source of truth for the public catalog.

Used by:
  - tools/dump_mcp_schema.py  → snapshot to docs/mcp_schema.json (committed)
  - tests/integration/protocol/test_schema_snapshot.py → golden-file
    diff catches schema drift

Both consumers go through ``collect_catalog()`` so they always see the
same shape. Output is a plain dict with sorted, deterministic ordering —
git diffs read cleanly when a tool/resource/prompt is added or removed.

The catalog is the closest thing MCP has to a Swagger artefact: tools/
resources/prompts surface the same JSON Schema clients see at runtime,
but as a static file you can review in PRs without spinning up the
server.
"""
from __future__ import annotations

import asyncio
from typing import Any


async def _collect_async() -> dict[str, Any]:
    """Drive FastMCP's introspection methods in-process.

    We call into the same MCP API that Claude Code calls over the wire —
    no synthetic schema generation, no risk of drift between snapshot and
    real protocol output.
    """
    # Lazy import so callers that don't need the catalog don't pay for it.
    from orcho_mcp.instance import mcp
    from orcho_mcp.server import _register_handlers

    _register_handlers()  # idempotent on repeat call

    tools = await mcp.list_tools()
    resources = await mcp.list_resources()
    templates = await mcp.list_resource_templates()
    prompts = await mcp.list_prompts()

    return {
        "tools": sorted(
            (
                {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": t.inputSchema,
                    "outputSchema": getattr(t, "outputSchema", None),
                }
                for t in tools
            ),
            key=lambda d: d["name"],
        ),
        "resources": sorted(
            (
                {
                    "uri": str(r.uri),
                    "name": r.name,
                    "description": r.description or "",
                    "mimeType": r.mimeType,
                }
                for r in resources
            ),
            key=lambda d: d["uri"],
        ),
        "resourceTemplates": sorted(
            (
                {
                    "uriTemplate": str(t.uriTemplate),
                    "name": t.name,
                    "description": t.description or "",
                    "mimeType": t.mimeType,
                }
                for t in templates
            ),
            key=lambda d: d["uriTemplate"],
        ),
        "prompts": sorted(
            (
                {
                    "name": p.name,
                    "description": p.description or "",
                    "arguments": [
                        {
                            "name": a.name,
                            "description": a.description or "",
                            "required": a.required,
                        }
                        for a in (p.arguments or [])
                    ],
                }
                for p in prompts
            ),
            key=lambda d: d["name"],
        ),
    }


def collect_catalog() -> dict[str, Any]:
    """Synchronous wrapper around the introspection traversal.

    Safe to call from sync code (the dump script) and from sync tests.
    Internally drives an asyncio event loop because FastMCP's list_*
    methods are async.
    """
    return asyncio.run(_collect_async())


__all__ = ["collect_catalog"]
