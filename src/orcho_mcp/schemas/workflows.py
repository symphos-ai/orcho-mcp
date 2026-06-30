"""orcho_mcp.schemas.workflows — wire models for workflow recipes.

``orcho_workflows_list`` (and the ``orcho://workflows`` resource) return
a small catalogue of machine-readable recipes that describe how a
sequence of MCP tool calls fits together. The wire shape is delivered
to tools-only clients (which cannot read MCP resources) as the tool
result, and to resource-aware clients as the resource body — same
payload, two delivery channels.

A recipe is intentionally **descriptive, not executable**. The server
does not interpret it; the client agent reads the steps and decides
which tool to call next. This keeps the recipe surface additive
(future steps and step kinds can land without changing the supervisor
or run lifecycle) and keeps the LLM in the loop for every concrete
call.

Step shape uses a tagged discriminated union on ``kind``:

* ``tool`` — invoke a named MCP tool with literal or templated args
  (``${var}`` references resolve against the recipe's declared
  ``inputs``).
* ``branch`` — non-linear control hint. ``when`` carries a small
  ``key: value`` pattern over recipe-relative observations
  (typically ``status`` from a prior result); ``next`` points at
  another step ``id`` in the same recipe.
* ``resource`` — read or subscribe to an MCP resource URI. Recipes use this
  for compact live state such as ``orcho://runs/{run_id}/summary`` so agents do
  not pull raw event streams during normal monitoring.

Both step variants share a string ``id`` so branch targets can
resolve unambiguously inside the same recipe.

``format_version`` lives on both the recipe and the envelope. Bumping
the envelope version covers catalogue-level shape changes; bumping a
single recipe version covers a single recipe's contract drift. Both
start at ``1``.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class RecipeInput(BaseModel):
    """One declared input the recipe expects from the operator.

    Inputs are referenced inside step args as ``${name}`` placeholders.
    The catalogue is read-only; client agents resolve placeholders
    themselves before issuing tool calls.
    """

    name: str = Field(
        description=(
            "Input identifier. Matched verbatim against ``${name}``"
            " placeholders inside step args."
        ),
    )
    required: bool = Field(
        description=(
            "True when the recipe cannot proceed without an operator"
            " value. False when a sensible default exists at the"
            " calling tool boundary."
        ),
    )


class RecipeToolStep(BaseModel):
    """A step that maps to one MCP tool invocation.

    ``args`` values may carry ``${input_name}`` placeholders referring
    to the recipe's declared inputs. The server does not substitute
    these — the client agent does, immediately before calling the
    target tool.
    """

    kind: Literal["tool"] = "tool"
    id: str = Field(
        description=(
            "Step identifier. Unique within the recipe; used as"
            " a ``branch.next`` target and as a stable handle for"
            " clients that render the recipe as a checklist."
        ),
    )
    tool: str = Field(
        description="Name of the MCP tool to invoke (e.g. ``orcho_run_start``).",
    )
    args: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Literal or ``${var}``-templated argument values. Values"
            " stay as strings on the wire so the recipe contract"
            " does not collide with per-tool input schemas; the"
            " client coerces to the target tool's parameter types."
        ),
    )


class RecipeBranchStep(BaseModel):
    """A step that records a non-linear continuation.

    ``when`` is a small ``key: value`` pattern the client agent
    matches against a prior step's observed result (typically
    ``status`` from ``orcho_run_status``). When every key matches,
    the client jumps to the step named by ``next``. The server does
    not evaluate ``when`` — recipes describe the path, the agent
    walks it.
    """

    kind: Literal["branch"] = "branch"
    id: str = Field(
        description="Step identifier. Unique within the recipe.",
    )
    when: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Pattern the agent matches against the most recent"
            " observed payload before deciding to follow this"
            " branch. Empty dict means 'always take this branch'."
        ),
    )
    next: str = Field(
        description=(
            "Target step ``id`` to jump to when ``when`` matches."
            " Must resolve to another step inside the same recipe."
        ),
    )


class RecipeResourceStep(BaseModel):
    """A step that points the client agent at an MCP resource URI.

    Resource steps are descriptive like tool steps: the server never
    subscribes on behalf of the client. The agent decides whether its
    MCP client can subscribe; when it cannot, ``fallback_tool`` names the
    tool path that preserves the workflow.
    """

    kind: Literal["resource"] = "resource"
    id: str = Field(
        description="Step identifier. Unique within the recipe.",
    )
    uri: str = Field(
        description=(
            "MCP resource URI to read or subscribe to. ``{run_id}`` means"
            " the live run id returned by an earlier run-start/resume step."
        ),
    )
    purpose: str = Field(
        description=(
            "Short instruction for what the agent should learn from this"
            " resource and why it is preferred."
        ),
    )
    subscribe: bool = Field(
        default=False,
        description=(
            "True when clients that support ``resources/subscribe`` should"
            " subscribe and refresh on ``notifications/resources/updated``."
        ),
    )
    fallback_tool: str | None = Field(
        default=None,
        description=(
            "Tool to use when the client cannot read or subscribe to"
            " resources."
        ),
    )


RecipeStep = Annotated[
    RecipeToolStep | RecipeBranchStep | RecipeResourceStep,
    Field(discriminator="kind"),
]


class WorkflowRecipe(BaseModel):
    """One named workflow recipe.

    Recipes are descriptive: every concrete tool call still goes
    through the operator's LLM agent. The recipe gives that agent
    a deterministic skeleton (which tools, in which order, with
    which arguments) so different clients converge on the same
    multi-step behaviour.
    """

    name: str = Field(
        description=(
            "Recipe identifier. Lower-snake-case; used by the"
            " agent to refer to the recipe in subsequent messages."
        ),
    )
    description: str = Field(
        description=(
            "Short human-readable summary. Clients SHOULD surface"
            " this when listing available recipes."
        ),
    )
    inputs: list[RecipeInput] = Field(
        default_factory=list,
        description=(
            "Declared inputs the recipe consumes. Steps may"
            " reference inputs as ``${name}`` placeholders inside"
            " their ``args``."
        ),
    )
    steps: list[RecipeStep] = Field(
        default_factory=list,
        description=(
            "Ordered step list. Step ``id`` values are unique"
            " within the recipe; ``branch.next`` always points at"
            " another step in this list."
        ),
    )
    format_version: int = Field(
        default=1,
        description=(
            "Schema revision for this individual recipe. Bumped"
            " when a recipe's own step shape changes in a way"
            " clients need to adapt to. Independent of the"
            " envelope ``format_version``."
        ),
    )


class WorkflowRecipeList(BaseModel):
    """Envelope for the recipe catalogue.

    Carried both as the ``orcho_workflows_list`` tool result and as
    the ``orcho://workflows`` resource body — the contract is the
    same regardless of delivery channel.
    """

    format_version: int = Field(
        default=2,
        description=(
            "Catalogue-level schema revision. Bumped when the"
            " envelope or step-union shape changes in a way that"
            " is not backwards-compatible for clients."
        ),
    )
    recipes: list[WorkflowRecipe] = Field(
        default_factory=list,
        description="Recipe entries; order is stable across calls.",
    )


__all__ = [
    "RecipeBranchStep",
    "RecipeInput",
    "RecipeResourceStep",
    "RecipeStep",
    "RecipeToolStep",
    "WorkflowRecipe",
    "WorkflowRecipeList",
]
