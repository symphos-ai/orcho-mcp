"""L1 unit — NextActionRecord call-readiness shape (T0).

The ``kind`` family is additive MCP-wire enrichment: a historical
pass-through record carrying only intent/tool/args/optional must still
deserialize cleanly and default to a ready-to-forward call.
"""
from orcho_mcp.schemas.shared import NextActionRecord


def test_legacy_record_defaults_to_ready_call():
    """A record without the new fields stays valid and ready_call."""
    record = NextActionRecord.model_validate(
        {
            "intent": "Resume the run",
            "tool": "orcho_run_resume",
            "args": {"run_id": "run-123"},
            "optional": False,
        }
    )

    assert record.kind == "ready_call"
    assert record.requires_operator_input is False
    assert record.choices is None
    assert record.input_schema is None


def test_minimal_record_defaults():
    """Even the barest record (intent + tool) lands on ready_call."""
    record = NextActionRecord(intent="Inspect status", tool="orcho_run_status")

    assert record.kind == "ready_call"
    assert record.requires_operator_input is False
    assert record.args == {}
    assert record.optional is True


def test_operator_input_required_record_round_trips():
    """The operator-input variant accepts choices and input_schema."""
    record = NextActionRecord(
        intent="Decide the phase handoff",
        tool="orcho_phase_handoff_decide",
        args={"run_id": "run-9", "handoff_id": "h-1"},
        kind="operator_input_required",
        requires_operator_input=True,
        choices=["continue", "halt", "retry_feedback"],
        input_schema={"feedback": {"type": "string"}},
    )

    dumped = record.model_dump()
    assert dumped["kind"] == "operator_input_required"
    assert dumped["requires_operator_input"] is True
    assert dumped["choices"] == ["continue", "halt", "retry_feedback"]
    assert dumped["input_schema"] == {"feedback": {"type": "string"}}
    assert "action" not in dumped["args"]
