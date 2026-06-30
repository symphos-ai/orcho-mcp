"""L3 stdio wire contract for the inspect_only control refusal.

End-to-end through a real ``python -m orcho_mcp`` subprocess + MCP
``ClientSession``: a foreign / CLI-started run dir (only ``meta.json``, no
``mcp_supervisor.json``) is ``inspect_only``, so the mutation tools must refuse
it and the client must receive the **typed** refusal over the wire — not just an
opaque error string.

This is the layer the unit / direct-call tests cannot reach: it proves the
``InspectOnlyControlError`` raised in the domain survives FastMCP's error
wrapping as a structured ``isError`` ``CallToolResult`` whose
``structuredContent`` carries ``kind`` / ``attempted`` / ``control`` and the
read-only ``next_actions`` (``orcho_run_status`` / ``orcho_run_evidence``), with
no spawn fields and no resume-of-self. Without the
``tool_error_delivery`` wrapper the payload would collapse to ``str(exc)`` and
this test would see ``structuredContent is None``.

``orcho_run_diagnose`` exposes the same classification on the success channel
(``control`` field), so the client can branch before ever attempting a mutation.

Transport plumbing comes from
``tests.fixtures.stdio.initialized_stdio_session``; run state is seeded with
``write_run`` (no real pipeline subprocess needed — the boundary is decided from
durable on-disk metadata alone).
"""
from __future__ import annotations

import pytest

pytest.importorskip("mcp.client.stdio")

from tests.fixtures.mcp_workspace import meta, write_run  # noqa: E402
from tests.fixtures.stdio import initialized_stdio_session  # noqa: E402

_FOREIGN_RESUMABLE = "20260101_000301"
_FOREIGN_PAUSED = "20260101_000302"
_HANDOFF = {
    "id": "validate_plan:plan_round:1",
    "phase": "validate_plan",
    "available_actions": ["continue", "retry_feedback", "halt"],
}


def _assert_wire_inspect_only(result, *, run_id: str, attempted: str) -> None:
    """Pin the structured inspect_only refusal as the client sees it on the wire."""
    # The refusal rides the error channel, but as STRUCTURED data — not a bare
    # string. Both must hold: isError marks the refusal, structuredContent
    # carries the typed payload.
    assert result.isError is True, "inspect_only refusal must be an error result"
    payload = result.structuredContent
    assert payload is not None, (
        "typed inspect_only payload must reach the client as structuredContent, "
        "not collapse to an opaque error string"
    )
    assert payload["kind"] == "inspect_only"
    assert payload["control"] == "inspect_only"
    assert payload["attempted"] == attempted
    assert payload["run_id"] == run_id

    # No spawn fields ever ride this shape.
    assert {"pid", "run_dir", "command", "started_at"}.isdisjoint(payload), payload

    # The CLI-control instruction lives in free text only.
    assert "CLI" in payload["message"]
    assert "CLI" in payload["suggested_next_action"]

    # next_actions: read-only MCP inspection ONLY — every record a ready_call to
    # orcho_run_status / orcho_run_evidence, never a resume of this run, never a
    # non-MCP / CLI tool.
    next_actions = payload["next_actions"]
    assert next_actions, "inspect_only refusal must offer inspection next_actions"
    tools = {na["tool"] for na in next_actions}
    assert tools <= {"orcho_run_status", "orcho_run_evidence"}, tools
    assert "orcho_run_resume" not in tools
    for na in next_actions:
        assert na["kind"] == "ready_call"
        assert na["args"].get("run_id") == run_id
        if na["tool"] == "orcho_run_evidence":
            assert na["args"].get("slice") == "errors", na["args"]


@pytest.mark.anyio
async def test_stdio_resume_foreign_run_delivers_typed_inspect_only(fake_workspace):
    """``orcho_run_resume`` on a foreign run dir → typed inspect_only over stdio."""
    # Otherwise-resumable (interrupted) but foreign: no mcp_supervisor.json.
    write_run(
        fake_workspace, _FOREIGN_RESUMABLE,
        meta=meta(status="interrupted", project="/p/x", task="foreign resume"),
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        # diagnose surfaces the same classification on the SUCCESS channel.
        diag = await session.call_tool(
            "orcho_run_diagnose", {"run_id": _FOREIGN_RESUMABLE},
        )
        assert diag.isError is False
        assert diag.structuredContent is not None
        assert diag.structuredContent["control"] == "inspect_only"
        assert diag.structuredContent["control_reason"]

        result = await session.call_tool(
            "orcho_run_resume", {"run_id": _FOREIGN_RESUMABLE},
        )
        _assert_wire_inspect_only(
            result, run_id=_FOREIGN_RESUMABLE, attempted="resume",
        )


@pytest.mark.anyio
async def test_stdio_decide_foreign_run_delivers_typed_inspect_only(fake_workspace):
    """``orcho_phase_handoff_decide`` on a foreign run dir → typed inspect_only."""
    # Paused on a handoff but foreign: no mcp_supervisor.json.
    write_run(
        fake_workspace, _FOREIGN_PAUSED,
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x",
            task="foreign decide", phase_handoff=_HANDOFF,
        ),
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_phase_handoff_decide",
            {
                "run_id": _FOREIGN_PAUSED,
                "handoff_id": _HANDOFF["id"],
                "action": "continue",
            },
        )
        _assert_wire_inspect_only(
            result, run_id=_FOREIGN_PAUSED, attempted="phase_handoff_decide",
        )
        # No decision artifact was written for a run MCP did not start.
        decisions = (
            fake_workspace / "runspace" / "runs" / _FOREIGN_PAUSED
            / "phase_handoff_decisions"
        )
        assert not decisions.exists()


@pytest.mark.anyio
async def test_stdio_controllable_success_shape_unchanged(fake_workspace):
    """A success call keeps its top-level structured shape (no error wrapper).

    Guards the round-1 invariant from the wire side: the inspect_only delivery
    wrapper must not perturb ordinary success results. ``orcho_run_status`` is a
    read tool whose structured payload must stay a non-error, top-level
    ``structuredContent`` dict.
    """
    write_run(
        fake_workspace, "20260101_000303",
        meta=meta(status="running", project="/p/x", task="controllable read"),
    )
    async with initialized_stdio_session(fake_workspace) as (session, _):
        ok = await session.call_tool(
            "orcho_run_status", {"run_id": "20260101_000303"},
        )
        assert ok.isError is False
        assert ok.structuredContent is not None
        assert ok.structuredContent["run_id"] == "20260101_000303"
