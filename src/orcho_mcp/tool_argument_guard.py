"""orcho_mcp.tool_argument_guard — reject undeclared tool arguments before dispatch.

MCP argument validation is *permissive about extra keys* at every layer of the
SDK we sit on (mcp 1.29.x):

* FastMCP registers the lowlevel handler as
  ``call_tool(validate_input=False)`` (``fastmcp/server.py``), so the incoming
  ``arguments`` are never checked against the published ``inputSchema``;
* the per-tool argument model FastMCP generates derives from ``ArgModelBase``,
  whose ``model_config`` sets only ``arbitrary_types_allowed`` — pydantic's
  default ``extra="ignore"`` therefore applies;
* neither ``FastMCP.tool()`` nor ``Tool.from_function`` exposes a knob to
  forbid extra keys.

The consequence is not a cosmetic one. A client that invents or misspells an
argument name gets **silent coercion**: the unknown key is dropped and the call
proceeds with every declared default in force. ``orcho_run_start(project="x")``
does not fail — it starts a real run against the server process's own working
directory. The mistake surfaces as a wrong run rather than as an error.

This module supplies the pre-dispatch check that closes that gap. It is
deliberately *not* a schema change: adding ``additionalProperties: false`` to
the published ``inputSchema`` would alter the wire catalog every client already
caches. The refusal is a runtime decision delivered as data.

Two pieces, kept separate so the decision is testable without a server:

* :func:`declared_argument_names` resolves what a tool accepts from the live
  FastMCP instance — the very ``Tool.parameters`` object that
  ``FastMCP.list_tools`` publishes as ``inputSchema``, so the accepted names in
  the refusal are exactly what the caller can already see in ``tools/list``
  (and never the Python signature, whose ``Context`` parameter FastMCP hides
  from the schema).
* :func:`unknown_arguments_result` is pure: tool name, arguments, declared
  names in, ``CallToolResult`` or ``None`` out.

An **unknown tool name** is not this module's business: it returns ``None`` so
FastMCP's own ``Unknown tool`` error is preserved unchanged.

Installed by :mod:`orcho_mcp.tool_error_delivery`, which owns the single
``CallToolRequest`` wrapper.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from mcp import types
from mcp.server.fastmcp import FastMCP

from orcho_mcp.schemas import UnknownArgumentsResult


def declared_argument_names(mcp: FastMCP, tool_name: str) -> list[str] | None:
    """Argument names ``tool_name`` publishes, in ``inputSchema`` order.

    Reads ``Tool.parameters`` from the tool manager — the same object
    ``FastMCP.list_tools`` hands out as ``inputSchema``, so the list is
    byte-for-byte the client's own view of the tool. ``None`` means the tool is
    not registered at all (the caller must then leave the call alone so FastMCP
    raises its normal ``Unknown tool`` error); ``[]`` means a registered tool
    that declares no arguments.
    """
    tool = mcp._tool_manager.get_tool(tool_name)  # noqa: SLF001 — shared handle, as elsewhere
    if tool is None:
        return None
    properties = tool.parameters.get("properties")
    if not isinstance(properties, dict):
        return []
    return list(properties)


def _refusal_message(
    tool_name: str,
    unknown: Sequence[str],
    accepted: Sequence[str],
) -> str:
    """One-line refusal: the tool, what was not recognised, what is."""
    accepted_text = ", ".join(accepted) if accepted else "(this tool takes no arguments)"
    return (
        f"{tool_name}: unknown argument(s): {', '.join(unknown)}. "
        f"Accepted arguments: {accepted_text}. "
        "Nothing was executed; retry with only the accepted names."
    )


def unknown_arguments_result(
    tool_name: str,
    arguments: Mapping[str, object],
    accepted_arguments: Sequence[str],
) -> types.CallToolResult | None:
    """Refuse ``arguments`` carrying names outside ``accepted_arguments``.

    Returns ``None`` — the "nothing to see here" signal — when every incoming
    key is declared, including the common case of an empty call. Otherwise
    returns an ``isError`` ``CallToolResult`` whose ``structuredContent`` is a
    serialized :class:`~orcho_mcp.schemas.UnknownArgumentsResult`.

    Compares **keys only**. Nothing is normalised, defaulted, renamed, or
    removed, and ``arguments`` is never copied, so a call with only declared
    names reaches the tool byte-identical to before this guard existed.
    """
    declared = list(accepted_arguments)
    known = set(declared)
    unknown = sorted(key for key in arguments if key not in known)
    if not unknown:
        return None

    refusal = UnknownArgumentsResult(
        tool=tool_name,
        unknown_arguments=unknown,
        accepted_arguments=declared,
        message=_refusal_message(tool_name, unknown, declared),
    )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=refusal.message)],
        structuredContent=refusal.model_dump(mode="json"),
        isError=True,
    )


def unknown_arguments_refusal(
    mcp: FastMCP,
    tool_name: str,
    arguments: Mapping[str, object],
) -> types.CallToolResult | None:
    """Bind :func:`unknown_arguments_result` to a live server's tool catalog.

    The entry point the ``CallToolRequest`` wrapper calls. ``None`` when the
    call is clean *or* when the tool is unregistered — in both cases dispatch
    must proceed untouched.
    """
    accepted = declared_argument_names(mcp, tool_name)
    if accepted is None:
        return None
    return unknown_arguments_result(tool_name, arguments, accepted)


__all__ = [
    "declared_argument_names",
    "unknown_arguments_refusal",
    "unknown_arguments_result",
]
