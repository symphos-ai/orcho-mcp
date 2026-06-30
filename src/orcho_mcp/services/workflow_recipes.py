"""orcho_mcp.services.workflow_recipes — machine-readable workflow catalogue.

Backs both the ``orcho_workflows_list`` tool and the
``orcho://workflows`` resource. The catalogue is hand-curated in
this module — no on-disk loader, no plugin registration — so the
recipe contract stays close to the wire schema and the test suite.

Each recipe is descriptive only. The server never executes a recipe;
it hands the steps to a client agent that decides which MCP tool
to call next. The recipes serve as a deterministic skeleton so
tools-only clients (which cannot read MCP resources) and
resource-aware clients converge on the same multi-step behaviour.

Placeholder convention: ``${name}`` inside a step's ``args`` always
refers to a recipe ``input`` declared on the same recipe. Arguments
the agent must populate from a prior step's result (most notably
``run_id`` values returned by ``orcho_run_start``) are intentionally
omitted from ``args`` — the recipe describes the call shape, the
agent threads the live values.

Companion prompt templates in :mod:`orcho_mcp.workflows` cover the
human-facing slash-command surface. The recipes here are the
machine-facing twin: same intent, parseable shape.
"""
from __future__ import annotations

from orcho_mcp.schemas.workflows import (
    RecipeBranchStep,
    RecipeInput,
    RecipeResourceStep,
    RecipeToolStep,
    WorkflowRecipe,
    WorkflowRecipeList,
)


def _plan_then_implement() -> WorkflowRecipe:
    """Two-step pipeline: planning profile, operator review, feature run."""
    return WorkflowRecipe(
        name="plan_then_implement",
        description=(
            "Spawn a planning-profile run, wait for the planning handoff, "
            "track compact run summaries while it advances, let the "
            "operator review the parsed plan, then spawn a "
            "feature-profile child run that inherits the plan via "
            "--from-run-plan."
        ),
        inputs=[
            RecipeInput(name="task", required=True),
            RecipeInput(name="project_dir", required=True),
        ],
        steps=[
            RecipeToolStep(
                id="start_plan",
                tool="orcho_run_start",
                args={
                    "task": "${task}",
                    "project_dir": "${project_dir}",
                    "profile": "planning",
                },
            ),
            RecipeResourceStep(
                id="monitor_plan_summary",
                uri="orcho://runs/{run_id}/summary",
                subscribe=True,
                fallback_tool="orcho_run_watch",
                purpose=(
                    "Preferred live state path after start_plan: subscribe "
                    "or refresh this compact summary to see validation "
                    "accept/reject, round changes, current_phase, and "
                    "next_seq without reading the full event stream."
                ),
            ),
            RecipeToolStep(
                id="watch_plan",
                tool="orcho_run_watch",
                args={"until": "handoff_or_terminal"},
            ),
            RecipeToolStep(
                id="review_plan",
                tool="orcho_run_evidence",
                args={"slice": "plan"},
            ),
            RecipeToolStep(
                id="start_implement",
                tool="orcho_run_start",
                args={
                    "project_dir": "${project_dir}",
                    "profile": "feature",
                },
            ),
            RecipeResourceStep(
                id="monitor_implement_summary",
                uri="orcho://runs/{run_id}/summary",
                subscribe=True,
                fallback_tool="orcho_run_watch",
                purpose=(
                    "Preferred live state path after start_implement: "
                    "subscribe or refresh the compact summary for phase "
                    "progress, handoff, and terminal status."
                ),
            ),
            RecipeToolStep(
                id="watch_implement",
                tool="orcho_run_watch",
                args={"until": "terminal"},
            ),
        ],
        format_version=2,
    )


def _review_paused_run() -> WorkflowRecipe:
    """Inspect a paused run and resolve its phase handoff."""
    return WorkflowRecipe(
        name="review_paused_run",
        description=(
            "Inspect a run paused on a phase handoff, surface the "
            "reviewer's findings, and resolve the handoff via "
            "continue, retry_feedback, continue_with_waiver, or halt."
        ),
        inputs=[
            RecipeInput(name="run_id", required=True),
        ],
        steps=[
            RecipeToolStep(
                id="status",
                tool="orcho_run_status",
                args={"run_id": "${run_id}"},
            ),
            RecipeBranchStep(
                id="ensure_paused",
                when={"status": "awaiting_phase_handoff"},
                next="findings",
            ),
            RecipeToolStep(
                id="findings",
                tool="orcho_run_evidence",
                args={
                    "run_id": "${run_id}",
                    "slice": "findings",
                },
            ),
            RecipeToolStep(
                id="decide",
                tool="orcho_phase_handoff_decide",
                args={"run_id": "${run_id}"},
            ),
            RecipeToolStep(
                id="resume",
                tool="orcho_run_resume",
                args={"run_id": "${run_id}"},
            ),
            RecipeResourceStep(
                id="monitor_resumed_summary",
                uri="orcho://runs/${run_id}/summary",
                subscribe=True,
                fallback_tool="orcho_run_watch",
                purpose=(
                    "Preferred live state after resume: subscribe or "
                    "refresh this compact summary instead of reading raw "
                    "events."
                ),
            ),
        ],
        format_version=2,
    )


def _resume_failed_run() -> WorkflowRecipe:
    """Restart a crashed run from its checkpoint after classifying state."""
    return WorkflowRecipe(
        name="resume_failed_run",
        description=(
            "Classify an interrupted run's terminal state and, when "
            "the state is checkpoint-resumable, continue it from the "
            "last successful phase boundary."
        ),
        inputs=[
            RecipeInput(name="run_id", required=True),
        ],
        steps=[
            RecipeToolStep(
                id="status",
                tool="orcho_run_status",
                args={"run_id": "${run_id}"},
            ),
            RecipeBranchStep(
                id="if_failed",
                when={"status": "failed"},
                next="resume",
            ),
            RecipeBranchStep(
                id="if_interrupted",
                when={"status": "interrupted"},
                next="resume",
            ),
            RecipeToolStep(
                id="resume",
                tool="orcho_run_resume",
                args={"run_id": "${run_id}"},
            ),
            RecipeResourceStep(
                id="monitor_resumed_summary",
                uri="orcho://runs/${run_id}/summary",
                subscribe=True,
                fallback_tool="orcho_run_watch",
                purpose=(
                    "Preferred live state after resume: subscribe or "
                    "refresh this compact summary; it carries status, "
                    "current_phase, next_seq, and bounded recent events."
                ),
            ),
            RecipeToolStep(
                id="watch",
                tool="orcho_run_watch",
                args={
                    "run_id": "${run_id}",
                    "until": "handoff_or_terminal",
                },
            ),
        ],
        format_version=2,
    )


def _inspect_terminal_run() -> WorkflowRecipe:
    """Pull the standard read-side slices for a finished run."""
    return WorkflowRecipe(
        name="inspect_terminal_run",
        description=(
            "Surface the canonical read-side view of a finished run: "
            "status, metrics, evidence slices, and the captured diff."
        ),
        inputs=[
            RecipeInput(name="run_id", required=True),
        ],
        steps=[
            RecipeToolStep(
                id="status",
                tool="orcho_run_status",
                args={"run_id": "${run_id}"},
            ),
            RecipeToolStep(
                id="metrics",
                tool="orcho_run_metrics",
                args={"run_id": "${run_id}"},
            ),
            RecipeToolStep(
                id="evidence",
                tool="orcho_run_evidence",
                args={
                    "run_id": "${run_id}",
                    "slice": "all",
                },
            ),
            RecipeToolStep(
                id="diff",
                tool="orcho_run_diff",
                args={
                    "run_id": "${run_id}",
                    "mode": "preview",
                },
            ),
        ],
    )


def _diagnose_halted_run() -> WorkflowRecipe:
    """Inspect a run that stopped before normal completion."""
    return WorkflowRecipe(
        name="diagnose_halted_run",
        description=(
            "Diagnose a halted run by reading its status, halt reason, "
            "error-focused evidence, and recent event summary before "
            "deciding whether the operator needs to clean project state "
            "or start a fresh run."
        ),
        inputs=[
            RecipeInput(name="run_id", required=True),
        ],
        steps=[
            RecipeToolStep(
                id="status",
                tool="orcho_run_status",
                args={"run_id": "${run_id}"},
            ),
            RecipeBranchStep(
                id="if_halted",
                when={"status": "halted"},
                next="errors",
            ),
            RecipeToolStep(
                id="errors",
                tool="orcho_run_evidence",
                args={
                    "run_id": "${run_id}",
                    "slice": "errors",
                },
            ),
            RecipeToolStep(
                id="events",
                tool="orcho_run_events_summary",
                args={"run_id": "${run_id}"},
            ),
            RecipeResourceStep(
                id="summary",
                uri="orcho://runs/${run_id}/summary",
                subscribe=False,
                fallback_tool="orcho_run_events_summary",
                purpose=(
                    "Read the compact latest event summary for recent "
                    "status and phase context; reserve the full events "
                    "resource for forensic replay only."
                ),
            ),
        ],
        format_version=2,
    )


def _observe_active_run() -> WorkflowRecipe:
    """Resilient short-watch + summary-fallback observation loop."""
    return WorkflowRecipe(
        name="observe_active_run",
        description=(
            "Follow an in-flight run with a resilient observation loop: a "
            "short bounded orcho_run_watch, then orcho_run_events_summary as "
            "the reconnect fallback, then watch again. Keep timeout_s short "
            "(120-240s) so each watch returns promptly with a fresh "
            "reconnect cursor. On a timeout trigger — or if the client "
            "transport drops the long-poll — call orcho_run_events_summary "
            "and resume watching from the cursor (summary.next_seq, or "
            "trigger.seq when summary=False). A disconnected watch is "
            "observer loss, not a run failure: the run keeps executing in "
            "its worktree, so take continue/retry/halt decisions only from "
            "the typed status / pending_handoff / terminal / evidence "
            "signals, never from a watch call ending early."
        ),
        inputs=[
            RecipeInput(name="run_id", required=True),
        ],
        steps=[
            RecipeToolStep(
                id="watch_bounded",
                tool="orcho_run_watch",
                args={
                    "run_id": "${run_id}",
                    "until": "subtask",
                    "timeout_s": "240",
                },
            ),
            RecipeBranchStep(
                id="if_timeout",
                when={"triggered": "false"},
                next="events_summary_fallback",
            ),
            RecipeToolStep(
                id="events_summary_fallback",
                tool="orcho_run_events_summary",
                args={"run_id": "${run_id}"},
            ),
            RecipeToolStep(
                id="rewatch",
                tool="orcho_run_watch",
                args={
                    "run_id": "${run_id}",
                    "until": "subtask",
                    "timeout_s": "240",
                },
            ),
        ],
        format_version=2,
    )


def _inspect_delivery_gate() -> WorkflowRecipe:
    """Classify an Orcho-managed run's post-release delivery / correction gate."""
    return WorkflowRecipe(
        name="inspect_delivery_gate",
        description=(
            "Decide what to do with a stopped Orcho-managed run WITHOUT "
            "parsing terminal prose or touching the retained diff by hand. "
            "Read orcho_run_status / orcho_run_diagnose, then orcho_delivery_gate "
            "for the typed gate kind, and branch on it: "
            "delivery_decision_required (pending delivery on an APPROVED "
            "release) and correction_decision_required (rejected release / "
            "fix_requested) are Orcho-managed gates — review the retained diff "
            "read-only via orcho_run_diff, then resolve the decision by "
            "choosing one ready orcho_delivery_decide call from the gate's "
            "next_actions (approve / apply / fix / skip / halt; approve is "
            "the only action that creates a commit). NEVER git apply / copy / "
            "commit the retained run worktree "
            "diff yourself for an Orcho-managed run, and never resume to force "
            "delivery: the SDK-backed orcho_delivery_decide tool is the only "
            "MCP mutation path for this gate. "
            "direct_checkout_or_running means there is NO Orcho delivery gate "
            "(a direct checkout edit, or a terminal / still-running run): there "
            "is nothing to deliver through Orcho — test and commit directly in "
            "the checkout."
        ),
        inputs=[
            RecipeInput(name="run_id", required=True),
        ],
        steps=[
            RecipeToolStep(
                id="status",
                tool="orcho_run_status",
                args={"run_id": "${run_id}"},
            ),
            RecipeToolStep(
                id="diagnose",
                tool="orcho_run_diagnose",
                args={"run_id": "${run_id}"},
            ),
            RecipeToolStep(
                id="gate",
                tool="orcho_delivery_gate",
                args={"run_id": "${run_id}"},
            ),
            RecipeBranchStep(
                id="if_delivery_decision",
                when={"kind": "delivery_decision_required"},
                next="review_retained_change",
            ),
            RecipeBranchStep(
                id="if_correction_decision",
                when={"kind": "correction_decision_required"},
                next="review_retained_change",
            ),
            RecipeBranchStep(
                id="if_direct_checkout",
                when={"kind": "direct_checkout_or_running"},
                next="review_direct_checkout",
            ),
            RecipeToolStep(
                # Orcho-managed gate path: review the retained worktree diff
                # READ-ONLY, then choose one ready orcho_delivery_decide call
                # from the gate projection. Do NOT apply or commit this diff by
                # hand — see the recipe description.
                id="review_retained_change",
                tool="orcho_run_diff",
                args={"run_id": "${run_id}", "mode": "preview"},
            ),
            RecipeToolStep(
                # Direct checkout path: no Orcho delivery gate exists, so review
                # the change and then test / commit directly in the checkout.
                id="review_direct_checkout",
                tool="orcho_run_diff",
                args={"run_id": "${run_id}", "mode": "preview"},
            ),
        ],
        format_version=1,
    )


def list_workflow_recipes() -> WorkflowRecipeList:
    """Return the deterministic catalogue of workflow recipes.

    Recipe order is stable across calls so clients that render the
    catalogue as a menu can rely on positions for keyboard shortcuts
    and persisted UI state.
    """
    return WorkflowRecipeList(
        recipes=[
            _plan_then_implement(),
            _review_paused_run(),
            _resume_failed_run(),
            _inspect_terminal_run(),
            _diagnose_halted_run(),
            _observe_active_run(),
            _inspect_delivery_gate(),
        ],
    )


__all__ = ["list_workflow_recipes"]
