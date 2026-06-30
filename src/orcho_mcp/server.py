"""orcho_mcp.server — entry point for the Orcho MCP server (stdio transport).

Registers the FastMCP server named "orcho" and imports the modules that
attach tools, resources, prompts, progress notifications, and workflow
helpers to the shared instance.

⚠️ stdio transport requires stdout to carry only protocol frames — never
``print()`` from inside this server. Logs go to stderr (Claude Code captures
it) or to a file. The FastMCP runtime already obeys this.
"""
from __future__ import annotations

import argparse
import sys

import anyio

from orcho_mcp import __version__
from orcho_mcp.instance import mcp
from orcho_mcp.observe.resource_subscriptions import (
    register_resource_subscription_handlers,
    run_stdio_with_resource_notifications,
)


def _register_handlers() -> None:
    """Side-effect import of the modules that decorate ``mcp`` with handlers.

    Importing ``tools`` etc. is what wires the @mcp.tool/@mcp.resource/@mcp.prompt
    decorators into the server instance. We do it inside main() (after the
    server object exists) rather than at module import time to keep
    ``orcho_mcp.server`` itself cheap to import for tests that only want
    the server instance.
    """
    from orcho_mcp import (
        resources,  # noqa: F401
        tools,  # noqa: F401  — importing for the decorator side-effect
    )

    # Prompt catalogue (_prompts/*.md). Dynamic — one prompt per file —
    # so registration is a function call rather than module import.
    from orcho_mcp.prompts import register_all_prompts
    register_all_prompts()

    # First-contact onboarding surface — orcho_getting_started prompt +
    # orcho://docs/getting-started resource, both backed by one markdown.
    # Workflow prompt templates (orcho_plan_then_implement,
    # orcho_followup_from_plan, orcho_review_paused_run,
    # orcho_halt_with_reason, orcho_resume_failed_run). Static — five
    # prompts total — so a side-effect import drives the @mcp.prompt
    # decorators on the canonical instance.
    from orcho_mcp import (
        onboarding,  # noqa: F401  — decorator side-effect
        workflows,  # noqa: F401  — decorator side-effect
    )
    register_resource_subscription_handlers(mcp)

    # Ship the typed inspect_only control refusal as structured error data on
    # the wire. Without this wrapper FastMCP collapses the raised
    # ``InspectOnlyControlError`` to ``str(exc)`` and the client loses the
    # typed classification + read-only next_actions. Must run after the tool
    # decorators are imported above so it wraps the live CallToolRequest
    # handler. Success results are unaffected.
    from orcho_mcp.tool_error_delivery import (
        register_inspect_only_error_delivery,
    )
    register_inspect_only_error_delivery(mcp)


def main(argv: list[str] | None = None) -> int:
    """Console-script entry. Parse args, then hand control to FastMCP's stdio loop."""
    parser = argparse.ArgumentParser(
        prog="orcho-mcp",
        description="Orcho — Model Context Protocol server",
    )
    parser.add_argument(
        "--version", action="version", version=f"orcho-mcp {__version__}",
    )
    parser.parse_args(argv)
    _register_handlers()
    anyio.run(run_stdio_with_resource_notifications, mcp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
