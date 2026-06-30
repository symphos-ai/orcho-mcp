"""orcho_mcp.authoring.plan_validation — plan-document validation entry.

Sync public function ``validate_plan_document`` backs the
``orcho_plan_validate`` MCP tool. Wraps
``pipeline.plan_parser.parse_plan`` — same semantics as the
pipeline's own DECOMPOSE_QA gate. Returns ``ok=False`` with a
human-readable error rather than raising for parse/DAG failures so
LLM callers can read the error and try again.

``InvalidPlanError`` is reserved for invalid arguments (both sources
or neither, or missing file path); parse / schema failures flow
through the typed ``ok=False`` shape instead.
"""
from __future__ import annotations

from pathlib import Path

from pipeline.plan_parser import PlanParseError, parse_plan

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.schemas import PlanValidateResult, SubTaskRecord


def validate_plan_document(
    markdown: str | None = None,
    path: str | None = None,
) -> PlanValidateResult:
    """Validate an architect plan document. See ``orcho_plan_validate``
    docstring in ``orcho_mcp.tools`` for the wire contract.

    Provide exactly one of ``markdown`` or ``path``. Returns
    ``PlanValidateResult(ok=False, error=...)`` on parse / schema
    failure so the caller can iterate; raises ``InvalidPlanError``
    only on argument-shape problems.
    """
    if (markdown is None) == (path is None):
        raise InvalidPlanError(
            "provide exactly one of 'markdown' or 'path' to orcho_plan_validate"
        )

    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise InvalidPlanError(f"plan file not found: {path}")
        markdown = p.read_text(encoding="utf-8")

    assert markdown is not None  # type-narrowing for the checker

    # Lazy import: PlanSchemaError lives in orcho-core's contracts module
    # and is only referenced here. Keeping the import lazy matches the
    # original tools.py shape and avoids pulling the schema module into
    # the import graph for callers that never validate plans.
    from core.contracts.plan_schema import PlanSchemaError

    try:
        plan = parse_plan(markdown)
    except (PlanParseError, PlanSchemaError) as e:
        return PlanValidateResult(ok=False, error=str(e))

    return PlanValidateResult(
        ok=True,
        source=plan.source,
        short_summary=plan.short_summary,
        planning_context=plan.planning_context,
        subtasks=[
            SubTaskRecord(
                id=t.id,
                goal=t.goal,
                spec=t.spec,
                files=list(t.files),
                skill=t.skill,
                model=t.model,
                depends_on=list(t.depends_on),
                done_criteria=list(t.done_criteria),
            )
            for t in plan.subtasks
        ],
    )


__all__ = ["validate_plan_document"]
