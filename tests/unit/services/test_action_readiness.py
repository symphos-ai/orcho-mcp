from __future__ import annotations

import pytest

from orcho_mcp.schemas import NextActionRecord
from orcho_mcp.services.action_readiness import ready_call_schema_errors
from orcho_mcp.services.delivery_gate import project_delivery_gate
from orcho_mcp.services.pending_decisions import project_pending_decisions
from orcho_mcp.tools import orcho_run_diagnose, orcho_run_live_status
from tests.fixtures.mcp_workspace import in_workspace_project, meta, write_run


@pytest.mark.asyncio
async def test_ready_calls_match_live_tools_list_required_args() -> None:
    """Every advertised ready call is checked against the actual MCP catalogue."""
    from orcho_mcp.instance import mcp

    tools = await mcp.list_tools()
    schemas = {tool.name: tool.inputSchema for tool in tools}
    actions = [
        NextActionRecord(
            intent="Inspect.", tool="orcho_run_status", args={"run_id": "r"},
            optional=False, kind="ready_call",
        ),
        NextActionRecord(
            intent="Choose correction.", tool="orcho_run_resume", args={"run_id": "r"},
            optional=False, kind="operator_input_required",
            requires_operator_input=True, choices=["followup", "exit"],
        ),
    ]

    assert ready_call_schema_errors(actions, schemas) == []


def test_incomplete_ready_call_is_rejected() -> None:
    errors = ready_call_schema_errors(
        [NextActionRecord(intent="Inspect.", tool="orcho_run_status", args={}, optional=False)],
        {"orcho_run_status": {"required": ["run_id"]}},
    )
    assert errors == ["orcho_run_status: missing required args ['run_id']"]


@pytest.mark.asyncio
async def test_emitted_read_actions_match_live_tool_schemas(fake_workspace) -> None:
    """Gate, diagnosis and workspace projections never advertise partial calls."""
    project = in_workspace_project(fake_workspace)
    write_run(
        fake_workspace, "gate",
        meta=meta(
            status="halted", project=project,
            commit_delivery={
                "status": "pending", "action": "approve",
                "release_verdict": "APPROVED",
            },
        ),
    )
    write_run(
        fake_workspace, "handoff",
        meta=meta(
            status="awaiting_phase_handoff", project=project,
            phase_handoff={
                "id": "validate:1", "phase": "validate_plan",
                "available_actions": ["continue", "halt"],
            },
        ),
    )

    from orcho_mcp.instance import mcp

    tools = await mcp.list_tools()
    schemas = {tool.name: tool.inputSchema for tool in tools}
    gate = project_delivery_gate("gate")
    diagnosis = orcho_run_diagnose("handoff")
    pending = project_pending_decisions(include_stale=True)
    live = orcho_run_live_status("handoff")
    actions = [*gate.next_actions, *diagnosis.next_actions]
    actions.extend(action for row in pending.runs for action in row.next_actions)

    assert live.run_id == "handoff"  # live status shares the same diagnosis input.
    assert ready_call_schema_errors(actions, schemas) == []
