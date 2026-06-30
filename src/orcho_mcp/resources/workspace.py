"""orcho_mcp.resources.workspace — ``orcho://workspace`` resource.

One MCP resource exposing where orcho reads/writes runs and which
projects appear in recent history. Backed by
``orcho_mcp.services.read_queries.get_workspace_info``.
"""
from __future__ import annotations

from orcho_mcp.instance import mcp
from orcho_mcp.resources.helpers import _dump
from orcho_mcp.services.read_queries import get_workspace_info


@mcp.resource(
    "orcho://workspace",
    name="orcho_workspace",
    description="Resolved workspace dir, runs dir, and recent project paths.",
    mime_type="application/json",
)
def workspace_resource() -> str:
    return _dump(get_workspace_info())


__all__ = ["workspace_resource"]
