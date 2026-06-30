"""orcho_mcp.tool_error_delivery — wire delivery for the inspect_only refusal.

FastMCP collapses any exception raised inside a tool body into an ``isError``
``CallToolResult`` whose only payload is ``str(exc)``: the lowlevel
``call_tool`` handler catches every exception except
``UrlElicitationRequiredError`` and calls ``_make_error_result(str(e))``, so the
structured data on a custom exception is dropped on the floor. That is fine for
opaque errors, but the ``inspect_only`` control refusal MUST reach the client as
*typed* data — the classification (``kind`` / ``attempted`` / ``control``) and
the read-only ``next_actions`` are the whole point of the boundary (acceptance
criterion #4: an inspect-only run hands the client typed next actions that do
not imply MCP can resume it).

This module installs a thin wrapper around FastMCP's registered
``CallToolRequest`` handler. On :class:`InspectOnlyControlError` it returns a
``CallToolResult`` *verbatim* — the lowlevel server passes a returned
``CallToolResult`` straight through (``isinstance(results, CallToolResult)``),
so the typed :class:`InspectOnlyControlResult` rides on ``structuredContent``
WITHOUT being validated against (or widening) the tool's success
``outputSchema``. Success calls are untouched: they flow through the original
FastMCP dispatch exactly as before, so ``orcho_run_resume`` /
``orcho_phase_handoff_decide`` keep their byte-identical success wire shape.

Mirrors ``observe.resource_subscriptions.register_resource_subscription_handlers``'s
direct use of the shared ``mcp._mcp_server`` low-level handle.
"""
from __future__ import annotations

from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from orcho_mcp.errors import InspectOnlyControlError


def inspect_only_tool_result(
    error: InspectOnlyControlError,
) -> types.CallToolResult:
    """Map an inspect_only refusal to a structured ``isError`` tool result.

    ``structuredContent`` carries the typed :class:`InspectOnlyControlResult`
    (``kind`` / ``attempted`` / ``control`` / read-only ``next_actions``);
    ``content`` carries the human-readable message. Returning a
    ``CallToolResult`` makes the lowlevel server pass it through verbatim, so the
    success ``outputSchema`` is never touched and never validated against this
    payload.
    """
    payload = error.result.model_dump(mode="json", by_alias=True)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=error.result.message)],
        structuredContent=payload,
        isError=True,
    )


def _unwrap_inspect_only(
    error: BaseException,
) -> InspectOnlyControlError | None:
    """Find an :class:`InspectOnlyControlError` in an exception or its causes.

    FastMCP wraps a tool-body exception as ``ToolError(...) from exc``, so the
    original control refusal lives on ``__cause__``. Walk a short, bounded cause
    chain so both a direct raise and the wrapped form resolve, without risking a
    cycle.
    """
    current: BaseException | None = error
    for _ in range(5):
        if current is None:
            break
        if isinstance(current, InspectOnlyControlError):
            return current
        current = current.__cause__
    return None


def register_inspect_only_error_delivery(mcp: FastMCP) -> None:
    """Wrap the FastMCP ``CallToolRequest`` handler to ship inspect_only as data.

    Re-registers the low-level ``call_tool`` handler so it delegates to the
    existing FastMCP tool dispatch and, on :class:`InspectOnlyControlError`,
    returns a structured ``isError`` ``CallToolResult`` instead of letting the
    typed payload collapse to ``str(exc)``. Every other tool result — success or
    any other error — flows through unchanged. Call once during handler
    registration.
    """
    server = mcp._mcp_server  # noqa: SLF001 — shared low-level handle, as elsewhere

    async def _call_tool(name: str, arguments: dict[str, object]) -> object:
        try:
            return await mcp.call_tool(name, arguments)
        except ToolError as exc:
            refusal = _unwrap_inspect_only(exc)
            if refusal is not None:
                return inspect_only_tool_result(refusal)
            raise

    server.call_tool(validate_input=False)(_call_tool)


__all__ = [
    "inspect_only_tool_result",
    "register_inspect_only_error_delivery",
]
