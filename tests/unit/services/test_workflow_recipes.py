"""Unit tests for the workflow-recipe catalogue.

The catalogue ships as the result of ``orcho_workflows_list`` and as
the body of ``orcho://workflows``. Both surfaces wrap
``orcho_mcp.services.workflow_recipes.list_workflow_recipes``; these
tests pin the catalogue's structural contract so a stray edit cannot
quietly break either delivery channel.

The contract checked here is:

* The canonical recipe names are present, in stable order.
* Envelope ``format_version`` is ``2``; recipe versions are pinned.
* Step ``id`` values are unique within each recipe.
* Every ``RecipeBranchStep.next`` resolves to another step in the
  same recipe.
* Every ``${var}`` placeholder inside a step value resolves
  to a declared recipe input.
"""
from __future__ import annotations

import re

from orcho_mcp.schemas.workflows import (
    RecipeBranchStep,
    RecipeResourceStep,
    RecipeToolStep,
    WorkflowRecipeList,
)
from orcho_mcp.services.workflow_recipes import list_workflow_recipes

EXPECTED_RECIPE_NAMES = (
    "plan_then_implement",
    "review_paused_run",
    "resume_failed_run",
    "inspect_terminal_run",
    "diagnose_halted_run",
    "observe_active_run",
    "inspect_delivery_gate",
)

_PLACEHOLDER_RE = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _placeholders(value: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(value))


def test_catalogue_returns_workflow_recipe_list() -> None:
    """Envelope is a ``WorkflowRecipeList`` instance at v2."""
    listing = list_workflow_recipes()
    assert isinstance(listing, WorkflowRecipeList)
    assert listing.format_version == 2


def test_catalogue_contains_expected_recipe_names() -> None:
    """The canonical recipes ship; order is stable."""
    listing = list_workflow_recipes()
    names = tuple(r.name for r in listing.recipes)
    assert names == EXPECTED_RECIPE_NAMES


def test_every_recipe_has_expected_format_version() -> None:
    """Per-recipe ``format_version`` changes only deliberately."""
    expected = {
        "plan_then_implement": 2,
        "review_paused_run": 2,
        "resume_failed_run": 2,
        "inspect_terminal_run": 1,
        "diagnose_halted_run": 2,
        "observe_active_run": 2,
        "inspect_delivery_gate": 1,
    }
    listing = list_workflow_recipes()
    for recipe in listing.recipes:
        assert recipe.format_version == expected[recipe.name], (
            f"{recipe.name}: format_version={recipe.format_version!r}, "
            f"expected {expected[recipe.name]}"
        )


def test_step_ids_are_unique_within_each_recipe() -> None:
    """A ``branch.next`` target must resolve to exactly one step."""
    listing = list_workflow_recipes()
    for recipe in listing.recipes:
        ids = [step.id for step in recipe.steps]
        duplicates = {sid for sid in ids if ids.count(sid) > 1}
        assert not duplicates, (
            f"{recipe.name}: duplicate step ids {sorted(duplicates)}"
        )


def test_branch_next_targets_resolve_within_same_recipe() -> None:
    """Every ``RecipeBranchStep.next`` names another step id."""
    listing = list_workflow_recipes()
    for recipe in listing.recipes:
        ids = {step.id for step in recipe.steps}
        for step in recipe.steps:
            if isinstance(step, RecipeBranchStep):
                assert step.next in ids, (
                    f"{recipe.name}: branch step {step.id!r} points at "
                    f"unknown target {step.next!r}; defined ids: "
                    f"{sorted(ids)}"
                )


def test_arg_placeholders_resolve_to_declared_inputs() -> None:
    """Every ``${var}`` in a step value is a declared input."""
    listing = list_workflow_recipes()
    for recipe in listing.recipes:
        declared = {inp.name for inp in recipe.inputs}
        for step in recipe.steps:
            values: dict[str, str] = {}
            if isinstance(step, RecipeToolStep):
                values = step.args
            elif isinstance(step, RecipeResourceStep):
                values = {"uri": step.uri}
            for value_key, value in values.items():
                refs = _placeholders(value)
                missing = refs - declared
                assert not missing, (
                    f"{recipe.name}.{step.id}.{value_key}: references "
                    f"undeclared inputs {sorted(missing)}; declared: "
                    f"{sorted(declared)}"
                )


def test_live_monitoring_steps_prefer_summary_resource() -> None:
    """Workflow monitoring points at compact summaries, not raw events."""
    listing = list_workflow_recipes()
    summary_recipes = set()
    for recipe in listing.recipes:
        for step in recipe.steps:
            if not isinstance(step, RecipeResourceStep) or not step.subscribe:
                continue
            summary_recipes.add(recipe.name)
            assert step.uri.endswith("/summary")
            assert "/events" not in step.uri
            assert step.fallback_tool in {
                "orcho_run_watch",
                "orcho_run_events_summary",
            }
    assert summary_recipes >= {
        "plan_then_implement",
        "review_paused_run",
        "resume_failed_run",
    }


def test_inspect_delivery_gate_branches_on_all_three_kinds() -> None:
    """The delivery-gate recipe forks on every orcho_delivery_gate kind.

    delivery_decision_required and correction_decision_required must lead to a
    read-only review (resolved through an orcho_delivery_decide ready call from
    the gate), and direct_checkout_or_running to the direct-checkout path. The
    recipe must call orcho_delivery_gate and must NOT suggest a manual diff
    apply.
    """
    listing = list_workflow_recipes()
    recipe = next(
        r for r in listing.recipes if r.name == "inspect_delivery_gate"
    )

    tools = [s.tool for s in recipe.steps if isinstance(s, RecipeToolStep)]
    assert "orcho_delivery_gate" in tools, "recipe must read the typed gate"

    branch_kinds = {
        step.when.get("kind")
        for step in recipe.steps
        if isinstance(step, RecipeBranchStep)
    }
    assert branch_kinds == {
        "delivery_decision_required",
        "correction_decision_required",
        "direct_checkout_or_running",
    }

    desc = recipe.description.lower()
    # Explicit guidance for the Orcho-managed gate vs. the direct checkout edit.
    assert "orcho_delivery_decide" in desc
    assert "next_actions" in desc
    assert "direct" in desc
    # Hard prohibition on manual application of the retained diff.
    assert "never" in desc
    assert "git apply" in desc or "by hand" in desc or "manually" in desc


def test_observe_active_run_is_a_bounded_watch_summary_loop() -> None:
    """The resilient observation recipe pins the short-watch + summary
    fallback + reconnect loop, and frames a dropped watch as observer
    loss rather than a run failure."""
    listing = list_workflow_recipes()
    recipe = next(
        r for r in listing.recipes if r.name == "observe_active_run"
    )

    tool_steps = [s for s in recipe.steps if isinstance(s, RecipeToolStep)]

    # A bounded orcho_run_watch with timeout_s in the 120..240 window,
    # carried as a string per the dict[str, str] args contract.
    watch_steps = [s for s in tool_steps if s.tool == "orcho_run_watch"]
    assert watch_steps, "recipe must drive orcho_run_watch"
    for step in watch_steps:
        timeout = step.args.get("timeout_s")
        assert timeout is not None, f"{step.id}: watch must bound timeout_s"
        assert isinstance(timeout, str)
        assert 120 <= int(timeout) <= 240, (
            f"{step.id}: timeout_s={timeout!r} outside the 120..240 "
            "bounded-watch window"
        )

    # A single-shot orcho_run_live_status progress read anchors the loop —
    # the progress / where-is-the-run intent routes here, not to run_status.
    assert any(
        s.tool == "orcho_run_live_status" for s in tool_steps
    ), "recipe must offer a single-shot orcho_run_live_status progress read"

    # An orcho_run_events_summary fallback step for reconnect.
    assert any(
        s.tool == "orcho_run_events_summary" for s in tool_steps
    ), "recipe must fall back to orcho_run_events_summary"

    # The description frames disconnect as observer loss, not run failure,
    # and points at the reconnect cursor.
    desc = recipe.description.lower()
    assert "observer loss" in desc
    assert "not a run failure" in desc or "not a failed run" in desc
    assert "next_seq" in desc or "trigger.seq" in desc
