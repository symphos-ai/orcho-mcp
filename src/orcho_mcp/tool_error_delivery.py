"""orcho_mcp.tool_error_delivery — the single ``CallToolRequest`` wrapper.

This module owns the one and only re-registration of the lowlevel
``CallToolRequest`` handler in this server, and it carries exactly two
responsibilities, both of which exist because FastMCP's default dispatch would
otherwise lose information the client needs:

(a) **Refusing undeclared arguments before dispatch.** Nothing in the SDK
    rejects argument names a tool does not declare — they are silently dropped
    and the call runs with defaults. The check from
    :mod:`orcho_mcp.tool_argument_guard` runs BEFORE ``mcp.call_tool``, hence
    before pydantic coercion and before any tool body, so a misspelled
    ``project`` never starts a run.

(b) **Delivering the typed ``inspect_only`` refusal as data**, per the rest of
    this docstring.

Both land as ``isError`` ``CallToolResult`` objects returned verbatim. Every
other call — success or any other error — flows through FastMCP's dispatch
exactly as before.

On (b): FastMCP collapses any exception raised inside a tool body into an ``isError``
``CallToolResult`` whose only payload is ``str(exc)``: the lowlevel
``call_tool`` handler catches every exception except
``UrlElicitationRequiredError`` and calls ``_make_error_result(str(e))``, so the
structured data on a custom exception is dropped on the floor. That is fine for
opaque errors, but the ``inspect_only`` control refusal MUST reach the client as
*typed* data — the classification (``kind`` / ``attempted`` / ``control``) and
the read-only ``next_actions`` are the whole point of the boundary (acceptance
criterion #4: an inspect-only run hands the client typed next actions that do
not imply MCP can resume it).

So on :class:`InspectOnlyControlError` the wrapper returns a
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
from orcho_mcp.tool_argument_guard import unknown_arguments_refusal


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
    """Install the server's single ``CallToolRequest`` wrapper.

    Re-registers the low-level ``call_tool`` handler so that, around the
    existing FastMCP tool dispatch, it:

    * screens the incoming ``arguments`` for names the tool does not declare and
      returns the typed refusal *without dispatching* — no pydantic coercion, no
      tool body, no side effect; and
    * on :class:`InspectOnlyControlError`, returns a structured ``isError``
      ``CallToolResult`` instead of letting the typed payload collapse to
      ``str(exc)``.

    Every other tool result — success or any other error — flows through
    unchanged. Call once during handler registration; a second call would
    re-wrap the already-wrapped handler.
    """
    server = mcp._mcp_server  # noqa: SLF001 — shared low-level handle, as elsewhere

    async def _call_tool(name: str, arguments: dict[str, object]) -> object:
        # Pre-dispatch: an undeclared argument name is a refusal, not a
        # silently-dropped key. An unregistered tool yields ``None`` here so
        # FastMCP's own ``Unknown tool`` error survives; a clean call falls
        # through with ``arguments`` untouched.
        refused = unknown_arguments_refusal(mcp, name, arguments)
        if refused is not None:
            return refused
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
