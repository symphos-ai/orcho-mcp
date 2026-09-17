"""In-process contract for the undeclared-argument guard.

Drives the **registered** ``CallToolRequest`` handler on the real server
object, not the guard's pure helpers. That distinction is the whole point: the
guard only protects clients if it sits on the handler the lowlevel server
actually dispatches to, and a test that calls
``orcho_mcp.tool_argument_guard.unknown_arguments_result`` directly would still
pass if nobody had wired it up.

What is being pinned, and why it matters:

* Nothing in the MCP SDK rejects an argument name a tool does not declare.
  FastMCP registers ``call_tool(validate_input=False)``, and the argument model
  it generates inherits pydantic's default ``extra="ignore"``. Absent the
  guard, ``orcho_run_start(project="x")`` does not fail — it **starts a run**
  against ``project_dir="."``, the server process's own directory. The
  ``start_run`` sentinel and the empty ``runspace/runs`` assertion below are
  there to catch exactly that: the refusal must land *before* dispatch.
* The accepted-name list must come from the tool's published
  ``inputSchema.properties``, so the correction the client is handed matches
  what it already saw in ``tools/list``. The sweep proves that for every tool
  in the live catalog rather than for the two hand-picked ones.
* The guard must stay narrow. An unregistered tool name is FastMCP's business,
  and a well-formed call must reach its tool body untouched.

The L3 counterpart (``tests/integration/protocol/test_stdio_unknown_arguments``)
proves the same refusal survives the stdio wire.
"""
from __future__ import annotations

import pytest
from mcp import types

from orcho_mcp.discovery import collect_catalog
from orcho_mcp.instance import mcp
from tests.fixtures.mcp_workspace import meta, write_run


def _call_tool_handler():
    """The lowlevel ``CallToolRequest`` handler the running server dispatches to.

    ``_register_handlers()`` is the same entry point ``server.main()`` uses and
    is idempotent on repeat calls, so every test here sees the fully-registered
    catalog and the live wrapper.
    """
    from orcho_mcp.server import _register_handlers

    _register_handlers()
    return mcp._mcp_server.request_handlers[types.CallToolRequest]  # noqa: SLF001


async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
    """Invoke a tool exactly as an inbound ``tools/call`` frame would.

    Returns the ``CallToolResult`` unwrapped from the ``ServerResult`` envelope
    — the same object a client receives.
    """
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )
    return (await _call_tool_handler()(request)).root


def _result_text(result: types.CallToolResult) -> str:
    return "".join(
        block.text for block in result.content
        if isinstance(block, types.TextContent)
    )


# The live published catalog — the same ``mcp.list_tools()`` payload
# ``docs/mcp_schema.json`` is dumped from. Collected once at import so the
# sweep below parametrises per tool and names the offender on failure.
_CATALOG_TOOLS: list[tuple[str, list[str]]] = [
    (entry["name"], list((entry["inputSchema"] or {}).get("properties", {})))
    for entry in collect_catalog()["tools"]
]


@pytest.mark.asyncio
async def test_run_start_with_unknown_argument_refuses_before_any_side_effect(
    fake_workspace, monkeypatch,
) -> None:
    """``project`` instead of ``project_dir`` is refused; no run is started.

    The sentinel is the load-bearing assertion. Without the guard this call
    succeeds: ``project`` is silently dropped, ``project_dir`` falls back to its
    ``"."`` default, and a real mock run spawns against the server's cwd.
    """
    reached: list[str] = []

    async def _never(*args, **kwargs):
        reached.append("start_run")
        raise AssertionError("tool body reached")

    monkeypatch.setattr("orcho_mcp.tools.start_run", _never)

    result = await call_tool(
        "orcho_run_start", {"task": "t", "mock": True, "project": "x"},
    )

    assert result.isError is True
    text = _result_text(result)
    # The message names both halves of the correction: what was not recognised
    # and what the caller should have sent instead.
    assert "project" in text
    assert "project_dir" in text
    assert "Traceback" not in text

    payload = result.structuredContent
    assert payload is not None, (
        "the refusal must reach the client as typed data, not an opaque string"
    )
    assert payload["kind"] == "unknown_arguments"
    assert payload["tool"] == "orcho_run_start"
    assert "project" in payload["unknown_arguments"]
    assert "project_dir" in payload["accepted_arguments"]

    assert reached == [], "guard must refuse BEFORE the tool body runs"
    assert list((fake_workspace / "runspace" / "runs").iterdir()) == [], (
        "a refused call must not leave a run directory behind"
    )


@pytest.mark.asyncio
async def test_run_status_with_misspelled_run_id_is_refused() -> None:
    """``run_ib`` is refused and the message points at ``run_id``."""
    result = await call_tool("orcho_run_status", {"run_ib": "r"})

    assert result.isError is True
    text = _result_text(result)
    assert "run_ib" in text
    assert "run_id" in text

    payload = result.structuredContent
    assert payload is not None
    assert payload["kind"] == "unknown_arguments"
    assert payload["unknown_arguments"] == ["run_ib"]
    assert "run_id" in payload["accepted_arguments"]


@pytest.mark.parametrize(
    ("tool_name", "declared"),
    _CATALOG_TOOLS,
    ids=[name for name, _ in _CATALOG_TOOLS],
)
@pytest.mark.asyncio
async def test_every_tool_refuses_an_undeclared_argument(
    tool_name: str, declared: list[str],
) -> None:
    """Sweep: no tool in the catalog silently swallows an unknown key.

    Also pins the message contract per tool — the offending key plus the tool's
    *complete* published argument list, so a client can correct the call from
    the refusal alone without a second ``tools/list`` round-trip.
    """
    result = await call_tool(tool_name, {"__bogus__": 1})

    assert result.isError is True, f"{tool_name} accepted an undeclared argument"
    text = _result_text(result)
    assert "__bogus__" in text, text
    missing = [name for name in declared if name not in text]
    assert missing == [], f"{tool_name} refusal omits accepted names {missing}"


@pytest.mark.asyncio
async def test_declared_arguments_still_reach_the_tool(fake_workspace) -> None:
    """Positive control: a well-formed call is untouched by the guard.

    Read tools are the strictest witness here — a spurious refusal or a mangled
    argument dict would show up immediately as a missing/incorrect payload.
    """
    write_run(
        fake_workspace, "20260101_000401",
        meta=meta(status="done", project="/p/x", task="declared args"),
    )

    result = await call_tool("orcho_run_status", {"run_id": "20260101_000401"})

    assert result.isError is False, _result_text(result)
    assert result.structuredContent is not None
    assert result.structuredContent["run_id"] == "20260101_000401"


@pytest.mark.asyncio
async def test_unknown_tool_still_raises_the_sdk_error() -> None:
    """The guard stays out of the way of an unregistered tool name.

    It has no schema to compare against and must return control to FastMCP so
    the standard ``Unknown tool`` error reaches the client unchanged — a
    different failure mode than a bad argument, and clients distinguish them.
    """
    result = await call_tool("orcho_nope", {})

    assert result.isError is True
    text = _result_text(result)
    assert "Unknown tool" in text
    assert "orcho_nope" in text
    assert "unknown_arguments" not in text
    assert result.structuredContent is None
