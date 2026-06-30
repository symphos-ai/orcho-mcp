"""orcho_mcp — Model Context Protocol server for the Orcho pipeline engine.

Exposes orcho's runtime to MCP-aware clients (Claude Code, Cursor, Zed,
and other MCP-speaking tools) over stdio transport. The package exposes
read tools, run-control tools, resources, MCP prompts, progress
notifications, and workflow helpers.

Versioning follows the public Orcho release tags.
"""
__version__ = "0.1.0"
