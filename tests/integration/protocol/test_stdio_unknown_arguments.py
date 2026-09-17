"""L3 stdio wire contract for the undeclared-argument refusal.

End-to-end through a real ``python -m orcho_mcp`` subprocess + MCP
``ClientSession``: a call carrying an argument name the tool does not declare
must come back as a structured ``isError`` result, and must come back *without
having done anything*.

Why this layer is needed on top of the in-process test: the guard lives in the
lowlevel ``CallToolRequest`` wrapper, and only a real subprocess proves that
wrapper is installed on the handler the stdio server dispatches to, that a
handler-returned ``CallToolResult`` survives JSON-RPC framing with its
``structuredContent`` intact, and that the refusal is not silently reshaped by
output validation on the way out.

``orcho_run_start`` is called here **without** ``project_dir`` on purpose. If
the guard ever regresses, the SDK drops the unknown ``project`` key, the
``project_dir="."`` default applies, and the server starts a real run against
its own working directory — so the empty-``runspace/runs`` assertion is what
makes that regression loud instead of invisible. ``mock=true`` and the
``ORCHO_WORKSPACE`` pinned to the ``fake_workspace`` tmp tree keep the blast
radius of that hypothetical failure to a local mock run, not a live provider
call.

Transport plumbing comes from ``tests.fixtures.stdio.initialized_stdio_session``;
run state is seeded with ``write_run``.
"""
from __future__ import annotations

import pytest

pytest.importorskip("mcp.client.stdio")

from tests.fixtures.mcp_workspace import meta, write_run  # noqa: E402
from tests.fixtures.stdio import initialized_stdio_session  # noqa: E402

_SEEDED = "20260101_000501"


def _text(result) -> str:
    return "".join(
        block.text for block in result.content if getattr(block, "type", None) == "text"
    )


@pytest.fixture
def anyio_backend():
    # Pin to asyncio — pytest-anyio defaults try trio too, which we don't ship.
    return "asyncio"


@pytest.mark.anyio
async def test_stdio_run_start_unknown_argument_starts_nothing(fake_workspace):
    """``project`` instead of ``project_dir`` → typed refusal, zero runs created."""
    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_run_start", {"task": "smoke", "mock": True, "project": "x"},
        )

        assert result.isError is True
        text = _text(result)
        assert "project" in text
        assert "project_dir" in text, (
            "the refusal must name the argument the caller should have used"
        )
        assert "Traceback" not in text

        payload = result.structuredContent
        assert payload is not None, (
            "typed refusal must reach the client as structuredContent, not "
            "collapse to an opaque error string"
        )
        assert payload["kind"] == "unknown_arguments"
        assert payload["tool"] == "orcho_run_start"
        assert "project" in payload["unknown_arguments"]
        assert "project_dir" in payload["accepted_arguments"]

    # Checked after the session closes so a spawned supervisor child would have
    # had its chance to create the run directory.
    assert list((fake_workspace / "runspace" / "runs").iterdir()) == [], (
        "a refused orcho_run_start must not have started a run"
    )


@pytest.mark.anyio
async def test_stdio_run_status_misspelled_run_id_is_refused(fake_workspace):
    """``run_ib`` → refusal naming both the typo and ``run_id``."""
    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_run_status", {"run_ib": "20260101_000001"},
        )

        assert result.isError is True
        text = _text(result)
        assert "run_ib" in text
        assert "run_id" in text

        payload = result.structuredContent
        assert payload is not None
        assert payload["kind"] == "unknown_arguments"
        assert payload["unknown_arguments"] == ["run_ib"]
        assert "run_id" in payload["accepted_arguments"]


@pytest.mark.anyio
async def test_stdio_declared_arguments_keep_working(fake_workspace):
    """Control: a well-formed read call is unaffected by the guard.

    Pins that the wrapper's pre-dispatch check is inert on a clean call — same
    success shape, same top-level ``structuredContent``, no error wrapper.
    """
    write_run(
        fake_workspace, _SEEDED,
        meta=meta(status="done", project="/p/x", task="declared args over stdio"),
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool("orcho_run_status", {"run_id": _SEEDED})

        assert result.isError is False, _text(result)
        assert result.structuredContent is not None
        assert result.structuredContent["run_id"] == _SEEDED
