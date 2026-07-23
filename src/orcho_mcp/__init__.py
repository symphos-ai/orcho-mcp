"""orcho_mcp — Model Context Protocol server for the Orcho pipeline engine.

Exposes orcho's runtime to MCP-aware clients (Claude Code, Cursor, Zed,
and other MCP-speaking tools) over stdio transport. The package exposes
read tools, run-control tools, resources, MCP prompts, progress
notifications, and workflow helpers.

Versioning follows the public Orcho release tags.
"""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("orcho-mcp")
except PackageNotFoundError:
    __version__ = "0+unknown"
