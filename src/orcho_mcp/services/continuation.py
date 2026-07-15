"""Core-owned retained-change continuation projection for MCP reads."""
from __future__ import annotations

from typing import Any

from orcho_mcp.schemas import NextActionRecord
from orcho_mcp.services.run_artifacts import get_run_meta_raw
from orcho_mcp.services.run_lookup import find_run_dir
from orcho_mcp.services.status_merge import merged_meta

CORRECTION_RESUME_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "operator_intent": {"type": "string", "enum": ["followup", "exit"]},
        "operator_comment": {"type": "string", "minLength": 1},
    },
    "required": ["operator_intent"],
    "allOf": [{
        "if": {"properties": {"operator_intent": {"const": "followup"}}},
        "then": {"required": ["operator_comment"]},
    }],
}


def resolve_core_continuation(run_id: str):
    from pipeline.control.continuation import resolve_continuation_decision

    run_dir = find_run_dir(run_id)
    raw = get_run_meta_raw(run_id) or {}
    meta = merged_meta(raw, run_dir) if isinstance(raw, dict) else None
    return resolve_continuation_decision(
        run_id=run_id, meta=meta, parent_run_dir=run_dir,
    )


def correction_resume_action(decision: Any) -> NextActionRecord | None:
    """The sole actionable record for an unblocked retained change."""
    if decision.continuation_subject != "retained_change" or decision.blocked:
        return None
    return NextActionRecord(
        intent="Choose whether to launch the retained-change correction follow-up.",
        tool="orcho_run_resume",
        args={"run_id": decision.run_id},
        optional=False,
        kind="operator_input_required",
        requires_operator_input=True,
        choices=["followup", "exit"],
        input_schema=CORRECTION_RESUME_INPUT_SCHEMA,
        context={
            "continuation_subject": decision.continuation_subject,
            "blocked": decision.blocked,
            "diff_source": decision.diff_source,
            "reason": decision.reason,
        },
    )


__all__ = ["CORRECTION_RESUME_INPUT_SCHEMA", "correction_resume_action", "resolve_core_continuation"]
