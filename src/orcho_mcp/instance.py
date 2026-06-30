"""orcho_mcp.instance — the single FastMCP server instance.

Lives in its own module so every other file (server, tools, resources, …)
imports the same object regardless of how the server was launched. Without
this split, ``python -m orcho_mcp.server`` and ``orcho_mcp.tools`` end up
holding two distinct FastMCP instances (``__main__`` vs ``orcho_mcp.server``)
and decorator registrations go to the wrong one.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("orcho")
