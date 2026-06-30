"""orcho_mcp.resources.workflows — ``orcho://workflows`` resource.

One MCP resource exposing the machine-readable workflow-recipe
catalogue. Resource-aware clients (Claude Code, Cursor) read the
recipes here; tools-only clients call the
``orcho_workflows_list`` tool — both paths return the same
``WorkflowRecipeList`` envelope.

Backed by :func:`orcho_mcp.services.workflow_recipes.list_workflow_recipes`.
"""
from __future__ import annotations

from orcho_mcp.instance import mcp
from orcho_mcp.resources.helpers import _dump
from orcho_mcp.services.workflow_recipes import list_workflow_recipes


@mcp.resource(
    "orcho://workflows",
    name="orcho_workflows",
    description=(
        "Machine-readable catalogue of workflow recipes "
        "(plan-then-implement, review-paused-run, resume-failed-run, "
        "inspect-terminal-run)."
    ),
    mime_type="application/json",
)
def workflows_resource() -> str:
    return _dump(list_workflow_recipes())


__all__ = ["workflows_resource"]
