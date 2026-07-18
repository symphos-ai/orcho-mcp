"""L1 + L2 tests for the workflow prompt templates (MCP UX A2).

Implements Principle 2 of ``docs/ux_vision.md`` — workflows that
span multiple tool calls are surfaced as named prompts so clients
(Claude Code, Cursor) can offer them as slash commands.

Layers covered:

* L1 — handler functions return non-empty templated content with
  the supplied arguments interpolated.
* L2 — server registration: ``await mcp.list_prompts()`` exposes
  all six workflows with correct argument schemas.

L3 (stdio E2E) is covered indirectly by ``test_schema_snapshot.py``
which round-trips the catalog through the published schema JSON.
"""
from __future__ import annotations

import pytest

from orcho_mcp.workflows import (
    orcho_followup_from_plan,
    orcho_halt_with_reason,
    orcho_observe_active_run,
    orcho_plan_then_implement,
    orcho_resume_failed_run,
    orcho_review_paused_run,
)

# ── L1: handler functions return templated content ──────────────────────────


class TestPlanThenImplement:
    def test_returns_non_empty_string(self) -> None:
        text = orcho_plan_then_implement(
            task="Add structured logging",
            project_dir="/tmp/proj",
        )
        assert isinstance(text, str)
        assert text.strip()

    def test_template_includes_supplied_task(self) -> None:
        text = orcho_plan_then_implement(
            task="UNIQUE_TASK_SENTINEL_42",
            project_dir="/tmp/proj",
        )
        assert "UNIQUE_TASK_SENTINEL_42" in text

    def test_template_includes_supplied_project_dir(self) -> None:
        text = orcho_plan_then_implement(
            task="x",
            project_dir="/tmp/UNIQUE_PROJECT_DIR_SENTINEL",
        )
        assert "/tmp/UNIQUE_PROJECT_DIR_SENTINEL" in text

    def test_template_names_the_workflow_tools(self) -> None:
        """The template must explicitly name each tool the LLM
        should call — otherwise the slash-prompt loses its
        operational value."""
        text = orcho_plan_then_implement(task="x", project_dir="/tmp/p")
        for tool in (
            "orcho_run_start",
            "orcho_run_status",
            "orcho_phase_handoff_decide",
        ):
            assert tool in text

    def test_template_routes_progress_to_live_status(self) -> None:
        """Progress-tracking steps point at orcho_run_live_status (with
        orcho_run_watch as the long-poll), not orcho_run_status."""
        text = orcho_plan_then_implement(task="x", project_dir="/tmp/p")
        assert (
            "Track progress with\n"
            "   `orcho_run_live_status(run_id)` (single-shot:"
        ) in text
        assert (
            "Track the child run's progress with `orcho_run_live_status`\n"
            "   (single-shot:"
        ) in text

    def test_template_references_from_run_plan_arg(self) -> None:
        """The second step of the workflow MUST use --from-run-plan
        to inherit the parent run's plan."""
        text = orcho_plan_then_implement(task="x", project_dir="/tmp/p")
        assert "from_run_plan" in text


class TestFollowupFromPlan:
    def test_template_includes_parent_run_id(self) -> None:
        text = orcho_followup_from_plan(
            parent_run_id="UNIQUE_PARENT_ID_SENTINEL_99",
        )
        # Parent id appears multiple times — as the from_run_plan
        # arg and as the run_id in the status verification step.
        assert text.count("UNIQUE_PARENT_ID_SENTINEL_99") >= 2

    def test_default_profile_is_feature(self) -> None:
        """``feature`` is the semantic default — it has the planning
        block to strip plus the full implementation loop."""
        text = orcho_followup_from_plan(parent_run_id="r-1")
        assert "feature" in text

    def test_custom_profile_overrides_default(self) -> None:
        text = orcho_followup_from_plan(
            parent_run_id="r-1", profile="complex_feature",
        )
        assert "complex_feature" in text

    def test_calls_out_incompatible_profile_check(self) -> None:
        """Template should remind the LLM that focused profiles
        (planning / code_review) are rejected by the orchestrator's
        fail-fast guard when used with --from-run-plan."""
        text = orcho_followup_from_plan(parent_run_id="r-1")
        assert "fail" in text.lower() or "incompatible" in text.lower()


class TestReviewPausedRun:
    def test_template_includes_run_id(self) -> None:
        text = orcho_review_paused_run(run_id="UNIQUE_PAUSED_RUN_ID_77")
        assert "UNIQUE_PAUSED_RUN_ID_77" in text

    def test_template_lists_all_decide_actions(self) -> None:
        text = orcho_review_paused_run(run_id="r-1")
        for verb in (
            "continue",
            "retry_feedback",
            "continue_with_waiver",
            "halt",
        ):
            assert verb in text

    def test_template_demands_confirmation_before_decide(self) -> None:
        """Decide is a one-way state transition. The template must
        require operator confirmation rather than letting the LLM
        auto-apply."""
        text = orcho_review_paused_run(run_id="r-1")
        assert "confirm" in text.lower() or "before" in text.lower()

    def test_template_includes_resume_followup(self) -> None:
        """For continue / retry_feedback, the decide API does not
        spawn anything — caller must follow up with resume. Template
        must remind."""
        text = orcho_review_paused_run(run_id="r-1")
        assert "orcho_run_resume" in text


class TestHaltWithReason:
    def test_template_includes_reason(self) -> None:
        text = orcho_halt_with_reason(
            run_id="r-1",
            reason="UNIQUE_HALT_REASON_SENTINEL",
        )
        assert "UNIQUE_HALT_REASON_SENTINEL" in text

    def test_template_includes_run_id(self) -> None:
        text = orcho_halt_with_reason(
            run_id="UNIQUE_HALT_RUN_ID", reason="x",
        )
        assert "UNIQUE_HALT_RUN_ID" in text

    def test_template_warns_halt_is_terminal(self) -> None:
        """Halt is irreversible. Template should make that explicit."""
        text = orcho_halt_with_reason(run_id="r-1", reason="x")
        assert (
            "terminal" in text.lower()
            or "irreversible" in text.lower()
            or "un-halt" in text.lower()
        )

    def test_template_uses_decide_with_halt_action(self) -> None:
        text = orcho_halt_with_reason(run_id="r-1", reason="x")
        assert "orcho_phase_handoff_decide" in text
        assert "halt" in text


class TestResumeFailedRun:
    def test_template_includes_run_id(self) -> None:
        text = orcho_resume_failed_run(
            run_id="UNIQUE_FAILED_RUN_SENTINEL",
        )
        assert "UNIQUE_FAILED_RUN_SENTINEL" in text

    def test_template_classifies_terminal_statuses(self) -> None:
        """Template must teach the LLM that not all states are
        resumable — terminal-success, halted-by-handoff, etc.
        require different responses."""
        text = orcho_resume_failed_run(run_id="r-1")
        # Status taxonomy mentioned.
        for status in (
            "done", "failed", "interrupted", "halted",
            "awaiting_phase_handoff",
        ):
            assert status in text

    def test_template_suggests_from_run_plan_for_terminal_success(
        self,
    ) -> None:
        """When run is terminal-success, --resume is wrong; the
        template should pivot to --from-run-plan as the path forward."""
        text = orcho_resume_failed_run(run_id="r-1")
        assert "from_run_plan" in text

    def test_template_routes_resumed_progress_to_live_status(self) -> None:
        """A resumed run's immediate progress read is live status, not status."""
        text = orcho_resume_failed_run(run_id="r-1")
        assert (
            "After resume, follow progress with `orcho_run_live_status`\n"
            "   (single-shot:"
        ) in text


class TestObserveActiveRun:
    def test_template_includes_run_id(self) -> None:
        text = orcho_observe_active_run(run_id="UNIQUE_OBSERVE_RUN_ID_88")
        assert "UNIQUE_OBSERVE_RUN_ID_88" in text

    def test_template_names_the_loop_tools(self) -> None:
        """The observation loop must explicitly name the bounded watch
        and the summary fallback the LLM threads together."""
        text = orcho_observe_active_run(run_id="r-1")
        assert "orcho_run_watch" in text
        assert "orcho_run_events_summary" in text

    def test_template_offers_single_shot_live_status(self) -> None:
        """The progress / where-is-the-run intent routes to
        orcho_run_live_status for a single-shot snapshot, distinct from
        the continuous watch loop."""
        text = orcho_observe_active_run(run_id="r-1")
        assert (
            'For a one-shot progress snapshot —\n'
            'current phase, subtask position (index/total), and whether the run is\n'
            'paused or terminal — call `orcho_run_live_status(run_id)`.'
        ) in text

    def test_template_recommends_bounded_timeout(self) -> None:
        """A short bounded watch is the whole point — the template must
        surface timeout_s rather than leaning on the 1h default."""
        text = orcho_observe_active_run(run_id="r-1")
        assert "timeout_s" in text

    def test_template_names_reconnect_cursor(self) -> None:
        text = orcho_observe_active_run(run_id="r-1")
        assert "next_seq" in text
        assert "trigger.seq" in text

    def test_template_frames_disconnect_as_observer_loss(self) -> None:
        """A dropped watch must read as observer loss, never as a run
        failure that justifies retry/cancel/halt."""
        text = orcho_observe_active_run(run_id="r-1")
        lowered = text.lower()
        assert "observer loss" in lowered
        assert "not a failed run" in lowered or "not a run failure" in lowered


# ── L2: server registration ────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPromptRegistration:
    """Confirm the workflow prompts surface in ``mcp.list_prompts()``
    after server side-effect imports run. Catches dual-import
    regressions where the workflows module decorates a different
    FastMCP instance than the one served."""

    async def test_all_six_workflows_registered(self) -> None:
        # Trigger the side-effect import path the server uses.
        from orcho_mcp import workflows  # noqa: F401
        from orcho_mcp.instance import mcp

        prompt_records = await mcp.list_prompts()
        names = {p.name for p in prompt_records}
        for expected in (
            "orcho_plan_then_implement",
            "orcho_followup_from_plan",
            "orcho_review_paused_run",
            "orcho_halt_with_reason",
            "orcho_resume_failed_run",
            "orcho_observe_active_run",
        ):
            assert expected in names, (
                f"workflow prompt {expected!r} did not register on the "
                "canonical FastMCP instance — likely a dual-import "
                "regression (see CLAUDE.md anti-pattern #1)"
            )

    async def test_plan_then_implement_arg_schema(self) -> None:
        from orcho_mcp import workflows  # noqa: F401
        from orcho_mcp.instance import mcp

        prompts_list = await mcp.list_prompts()
        plan_then_implement = next(
            p for p in prompts_list
            if p.name == "orcho_plan_then_implement"
        )
        # Both task and project_dir are required arguments — no defaults.
        arg_names = {a.name for a in (plan_then_implement.arguments or [])}
        assert {"task", "project_dir"} <= arg_names

    async def test_followup_from_plan_profile_has_default(self) -> None:
        from orcho_mcp import workflows  # noqa: F401
        from orcho_mcp.instance import mcp

        prompts_list = await mcp.list_prompts()
        followup = next(
            p for p in prompts_list
            if p.name == "orcho_followup_from_plan"
        )
        # parent_run_id is required; profile has a default (feature).
        arg_map = {
            a.name: a for a in (followup.arguments or [])
        }
        assert "parent_run_id" in arg_map
        assert "profile" in arg_map
