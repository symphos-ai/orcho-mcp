"""Acceptance smoke for the durable controllability boundary (L4).

Pins the `mcp_controllable` / `inspect_only` boundary documented in
``docs/run_lifecycle.md`` against a real ``--mock`` subprocess plus a
synthetic *foreign* run dir:

  * An MCP-started run (spawned via ``orcho_run_start``) carries durable
    ``mcp_supervisor.json`` and ``orcho_run_diagnose`` reports
    ``control='mcp_controllable'``.

  * A foreign / CLI-started run dir — modelled by copying the started run's
    ``meta.json`` into a fresh run dir *without* ``mcp_supervisor.json`` — is
    ``inspect_only``: ``orcho_run_diagnose`` reports it, and the mutation tools
    refuse it *before* any spawn / SDK call by *raising*
    ``InspectOnlyControlError``. Raising (not widening the success return)
    keeps the mutation tools' success ``outputSchema`` unchanged. The carried
    ``result`` is the typed ``InspectOnlyControlResult`` whose ``next_actions``
    are read-only inspection only (``orcho_run_status`` / ``orcho_run_evidence``).
    The CLI-control instruction rides in ``message`` / ``suggested_next_action``,
    never as a next_action; ``orcho_phase_handoff_decide`` writes no decision
    artifact.

The test asserts BOTH halves of the contract: the in-process direct call
raises the typed error, AND a real MCP ``ClientSession`` (stdio) receives the
refusal as structured ``isError`` data (``structuredContent`` carries
``kind`` / ``attempted`` / ``control`` / read-only ``next_actions``) — proving
the payload survives FastMCP error wrapping on the wire, not just in Python.

Sibling of ``test_run_diagnosis_smoke.py`` / ``test_orcho_run_resume.py``;
follows the same L4 pattern (live tool surface over a real subprocess).
Marked ``mcp_integration`` so the default suite stays fast.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.mcp_integration

# ── fixtures: ``_supervisor_reset``, ``mock_project``, ``runs_dir`` live in
# the acceptance conftest.

_FOREIGN_RUN_ID = "20260101_000000_face00"


async def _wait_done(run_id: str, timeout_s: float = 90.0) -> str:
    """Poll ``orcho_run_status`` until the run reaches a terminal state."""
    from orcho_mcp.tools import orcho_run_status

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = orcho_run_status(run_id)
        cur = (snap.meta or {}).get("status")
        if cur in {"done", "failed", "interrupted", "halted"}:
            return cur
        await asyncio.sleep(0.3)
    raise AssertionError(
        f"run {run_id} did not reach a terminal status within {timeout_s}s"
    )


def _assert_wire_inspect_only(result, *, run_id: str, attempted: str) -> None:
    """Pin the structured inspect_only refusal as the CLIENT sees it on the wire.

    The refusal rides the MCP error channel (``isError``) but as STRUCTURED data
    on ``structuredContent`` — not a collapsed ``str(exc)``. This is the
    client-facing half of acceptance criterion #4: typed classification +
    read-only next_actions must survive FastMCP error wrapping.
    """
    assert result.isError is True, "inspect_only refusal must be an error result"
    payload = result.structuredContent
    assert payload is not None, (
        "typed inspect_only payload must reach the client as structuredContent"
    )
    assert payload["kind"] == "inspect_only"
    assert payload["control"] == "inspect_only"
    assert payload["attempted"] == attempted
    assert payload["run_id"] == run_id
    assert {"pid", "run_dir", "command", "started_at"}.isdisjoint(payload), payload
    assert "CLI" in payload["message"]
    assert "CLI" in payload["suggested_next_action"]
    tools = {na["tool"] for na in payload["next_actions"]}
    assert tools, "inspect_only refusal must offer inspection next_actions"
    assert tools <= {"orcho_run_status", "orcho_run_evidence"}, tools
    assert "orcho_run_resume" not in tools
    for na in payload["next_actions"]:
        assert na["kind"] == "ready_call"
        assert na["args"].get("run_id") == run_id
        if na["tool"] == "orcho_run_evidence":
            assert na["args"].get("slice") == "errors", na["args"]


def _assert_inspect_only(result, *, run_id: str, attempted: str) -> None:
    """Pin the typed InspectOnlyControlResult contract for a foreign run."""
    from orcho_mcp.schemas import InspectOnlyControlResult

    assert isinstance(result, InspectOnlyControlResult), type(result)
    assert result.kind == "inspect_only"
    assert result.control == "inspect_only"
    assert result.attempted == attempted
    assert result.run_id == run_id

    # No spawn fields in the public wire form.
    dumped = result.model_dump()
    assert {"pid", "run_dir", "command", "started_at"}.isdisjoint(dumped), (
        f"inspect_only result must carry no spawn fields; got {sorted(dumped)}"
    )

    # The CLI-control instruction lives only in free text — never a next_action.
    assert "CLI" in result.message
    assert "CLI" in result.suggested_next_action

    # next_actions: read-only MCP inspection ONLY — every record a ready_call to
    # orcho_run_status / orcho_run_evidence, none a resume of this run, none a
    # non-MCP / CLI tool.
    tools = [na.tool for na in result.next_actions]
    assert tools, "inspect_only result must offer inspection next_actions"
    assert set(tools) <= {"orcho_run_status", "orcho_run_evidence"}, tools
    assert "orcho_run_resume" not in tools
    assert all(na.kind == "ready_call" for na in result.next_actions)
    # Each record is a valid ready_call to the inspection tool it names: status
    # carries just the run_id; evidence MUST pin slice='errors' (the read-only
    # error slice). A builder regression that drops or changes the slice fails
    # here rather than passing the name-only checks above.
    for na in result.next_actions:
        assert na.args.get("run_id") == run_id
        if na.tool == "orcho_run_evidence":
            assert na.args.get("slice") == "errors", na.args


@pytest.mark.asyncio
async def test_foreign_run_dir_is_inspect_only_end_to_end(
    mock_project: Path,
    runs_dir: Path,
) -> None:
    from orcho_mcp.errors import InspectOnlyControlError
    from orcho_mcp.schemas import RunDiagnosis
    from orcho_mcp.tools import (
        orcho_phase_handoff_decide,
        orcho_run_diagnose,
        orcho_run_resume,
        orcho_run_start,
    )

    # ── MCP-started run: durable mcp_supervisor.json → mcp_controllable. ──
    started = await orcho_run_start(
        task="controllability boundary smoke — say hello",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    run_id = started.run_id
    assert started.pid > 0
    assert (await _wait_done(run_id)) == "done"

    controllable = orcho_run_diagnose(run_id)
    assert isinstance(controllable, RunDiagnosis), type(controllable)
    assert controllable.control == "mcp_controllable", controllable.control
    assert controllable.control_reason

    # The started run really does carry the durable state file.
    assert (Path(started.run_dir) / "mcp_supervisor.json").is_file()

    # ── Foreign run dir: same meta.json, NO mcp_supervisor.json → inspect_only.
    foreign_dir = runs_dir / _FOREIGN_RUN_ID
    foreign_dir.mkdir(parents=True)
    meta_bytes = (Path(started.run_dir) / "meta.json").read_bytes()
    (foreign_dir / "meta.json").write_bytes(meta_bytes)
    assert not (foreign_dir / "mcp_supervisor.json").exists()

    # diagnose classifies the foreign run as inspect_only on the same axis.
    foreign_diag = orcho_run_diagnose(_FOREIGN_RUN_ID)
    assert isinstance(foreign_diag, RunDiagnosis), type(foreign_diag)
    assert foreign_diag.control == "inspect_only", foreign_diag.control
    assert foreign_diag.control_reason

    # resume on the foreign run → typed inspect_only refusal raised before any
    # spawn; the success return shape is untouched (the refusal is an error).
    with pytest.raises(InspectOnlyControlError) as resume_exc:
        await orcho_run_resume(_FOREIGN_RUN_ID)
    _assert_inspect_only(
        resume_exc.value.result, run_id=_FOREIGN_RUN_ID, attempted="resume",
    )

    # decide on the foreign run → typed inspect_only refusal raised before any
    # SDK call, and NO decision artifact is written.
    with pytest.raises(InspectOnlyControlError) as decide_exc:
        await orcho_phase_handoff_decide(
            _FOREIGN_RUN_ID,
            handoff_id="validate_plan:plan_round:1",
            action="continue",
        )
    _assert_inspect_only(
        decide_exc.value.result,
        run_id=_FOREIGN_RUN_ID,
        attempted="phase_handoff_decide",
    )
    assert not (foreign_dir / "phase_handoff_decisions").exists(), (
        "inspect_only decide must not write a decision artifact"
    )

    # ── Wire contract: the CLIENT must receive the typed refusal as structured
    # error data over a real MCP stdio session — not just an exception raised
    # in-process. This is the half the direct-call asserts above cannot prove.
    from tests.fixtures.stdio import initialized_stdio_session

    workspace_dir = mock_project.parent
    async with initialized_stdio_session(workspace_dir) as (session, _):
        # diagnose carries the classification on the SUCCESS channel.
        diag = await session.call_tool(
            "orcho_run_diagnose", {"run_id": _FOREIGN_RUN_ID},
        )
        assert diag.isError is False
        assert diag.structuredContent is not None
        assert diag.structuredContent["control"] == "inspect_only"

        wire_resume = await session.call_tool(
            "orcho_run_resume", {"run_id": _FOREIGN_RUN_ID},
        )
        _assert_wire_inspect_only(
            wire_resume, run_id=_FOREIGN_RUN_ID, attempted="resume",
        )

        wire_decide = await session.call_tool(
            "orcho_phase_handoff_decide",
            {
                "run_id": _FOREIGN_RUN_ID,
                "handoff_id": "validate_plan:plan_round:1",
                "action": "continue",
            },
        )
        _assert_wire_inspect_only(
            wire_decide,
            run_id=_FOREIGN_RUN_ID,
            attempted="phase_handoff_decide",
        )

    # The over-the-wire decide still wrote no decision artifact.
    assert not (foreign_dir / "phase_handoff_decisions").exists(), (
        "inspect_only decide must not write a decision artifact (wire path)"
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
