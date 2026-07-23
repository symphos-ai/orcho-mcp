"""Validation for advertised ready calls against the live MCP tool schemas."""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from orcho_mcp.schemas import NextActionRecord


def ready_call_schema_errors(
    actions: Iterable[NextActionRecord],
    tool_schemas: Mapping[str, Mapping[str, object]],
) -> list[str]:
    """Return violations instead of allowing an incomplete call to be advertised.

    The validator deliberately checks only JSON Schema's universally available
    ``required`` contract.  Richer constraints remain the target tool's own
    validation concern; an action missing a required input must instead be an
    ``operator_input_required`` record.
    """
    errors: list[str] = []
    for action in actions:
        if action.kind != "ready_call":
            continue
        schema = tool_schemas.get(action.tool)
        if schema is None:
            errors.append(f"{action.tool}: tool schema not found")
            continue
        required = schema.get("required", [])
        if not isinstance(required, list):
            continue
        missing = [name for name in required if isinstance(name, str) and name not in action.args]
        if missing:
            errors.append(f"{action.tool}: missing required args {missing}")
    return errors


__all__ = ["ready_call_schema_errors"]
