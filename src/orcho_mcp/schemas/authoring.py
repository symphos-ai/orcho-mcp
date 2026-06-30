"""orcho_mcp.schemas.authoring — wire models for the authoring tool family.

Covers ``orcho_plan_validate`` (plan JSON / markdown validation) and
``orcho_prompts_resolve`` (project → workspace → core prompt-chain
resolution).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

# ── orcho_plan_validate ──────────────────────────────────────────────────────


class SubTaskRecord(BaseModel):
    id: str
    goal: str
    spec: str = ""
    files: list[str] = Field(default_factory=list)
    skill: str | None = None
    model: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    done_criteria: list[str] = Field(default_factory=list)


class PlanValidateResult(BaseModel):
    ok: bool
    source: str | None = Field(
        default=None,
        description='"json" if the plan came from a ```json``` fence, "markdown" if '
                    'from section headers. None when ok=False.',
    )
    short_summary: str | None = None
    planning_context: str | None = None
    subtasks: list[SubTaskRecord] = Field(default_factory=list)
    error: str | None = Field(
        default=None,
        description="Human-readable parse/DAG error. Set iff ok=False.",
    )


# ── orcho_prompts_resolve ────────────────────────────────────────────────────


class PromptChainEntry(BaseModel):
    level: str  # "project" | "workspace" | "core"
    path: str
    exists: bool


class PromptResolveResult(BaseModel):
    name: str
    chain: list[PromptChainEntry]
    resolved_path: str | None = Field(
        default=None,
        description="Path of the winning chain entry (first existing). None if no level exists.",
    )
    resolved_text: str | None = Field(
        default=None,
        description="Content of the winning entry. None if no level exists.",
    )


__all__ = [
    "PlanValidateResult",
    "PromptChainEntry",
    "PromptResolveResult",
    "SubTaskRecord",
]
