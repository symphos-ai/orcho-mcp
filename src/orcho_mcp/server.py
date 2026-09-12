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

    # The server's single CallToolRequest wrapper. It refuses a call carrying
    # argument names the tool does not declare (the SDK would otherwise drop
    # them silently and run the tool on its defaults), and ships the typed
    # inspect_only control refusal as structured error data instead of letting
    # FastMCP collapse the raised ``InspectOnlyControlError`` to ``str(exc)``.
    # Must run after the tool decorators are imported above so it wraps the
    # live handler and sees the full tool catalog. Success results are
    # unaffected. Registered exactly once per process.
    from orcho_mcp.tool_error_delivery import (
        register_inspect_only_error_delivery,
    )
    register_inspect_only_error_delivery(mcp)


def _recover_abandoned_runs() -> None:
    """Flip runs abandoned by a previous server process to ``orphaned``.

    A run is a detached child of this server, reaped by an asyncio task
    inside it. When the server process itself goes away — a client restart,
    or a kill that takes the whole process tree with it — that task dies
    with the process, and nothing is left to record that the run ended:
    ``mcp_supervisor.json`` keeps saying ``running``, ``meta.json`` keeps
    saying ``running``, and every read surface keeps reporting a run that
    ceased to exist as live work.

    The supervisor's recovery probe exists for exactly that, and only helps
    if something calls it. It runs once per server start, before the first
    client request, and touches only ``running`` entries whose recorded pid
    is proven dead — a live run under another supervisor is left alone, and
    a paused (``awaiting_phase_handoff``) run is not an orphan even though
    its pid is expected to be gone.

    Never fatal: a failed probe leaves exactly the stale state it was meant
    to clear, which is not a reason to refuse to start. Diagnostics go to
    stderr — stdio stdout carries protocol frames only.
    """
    from orcho_mcp.supervisor import get_supervisor

    try:
        orphaned = get_supervisor().recover()
    except Exception as exc:  # noqa: BLE001 — startup must survive any probe failure
        print(f"orcho-mcp: run recovery skipped ({exc})", file=sys.stderr)
        return
    if orphaned:
        print(
            f"orcho-mcp: marked {len(orphaned)} abandoned run(s) orphaned: "
            + ", ".join(orphaned),
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    """Console-script entry. Parse args, then hand control to FastMCP's stdio loop."""
    parser = argparse.ArgumentParser(
        prog="orcho-mcp",
        description=(
            "Orcho — Model Context Protocol server.\n\n"
            "Exposes the Orcho pipeline engine (run control, evidence, and "
            "inspection) to MCP-speaking clients such as Claude Code, Cursor, "
            "and Zed. It speaks JSON-RPC over stdio and is normally launched "
            "by the MCP client, not run directly in a terminal."
        ),
        epilog=(
            "typical use:\n"
            "  Register this command as an MCP server in your client, e.g.\n"
            "  Claude Code:  claude mcp add orcho -- orcho-mcp\n\n"
            "  Running it by hand starts the stdio server and waits for a\n"
            "  client on stdin/stdout — there is no interactive prompt.\n\n"
            "docs: https://docs.orcho.dev/start/let-your-agent-drive/"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"orcho-mcp {__version__}",
    )
    parser.parse_args(argv)
    _register_handlers()
    _recover_abandoned_runs()
    anyio.run(run_stdio_with_resource_notifications, mcp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
