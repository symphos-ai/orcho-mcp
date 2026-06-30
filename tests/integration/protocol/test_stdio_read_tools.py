"""L3 stdio smoke for read-only tools.

End-to-end through a real ``python -m orcho_mcp`` subprocess: spawn
the server, initialize the client session, ``call_tool`` against a
seeded fake workspace, assert the JSON-RPC response shape parses as
the expected Pydantic model. This is the layer that catches stdout
pollution, JSON-RPC framing regressions, and capability negotiation
bugs the in-process L1 tests miss.

Separated from ``test_initialize_handshake.py`` so the handshake file
stays scoped to catalog presence / stdio framing while this file
hosts ``call_tool`` smoke for read-side tools. Future read-tool L3
smokes (when added) land here.

Subprocess + session plumbing comes from
``tests.fixtures.stdio.initialized_stdio_session``; the tests below are
deliberately "call tool, assert payload" only.
"""
from __future__ import annotations

import pytest

pytest.importorskip("mcp.client.stdio")

from tests.fixtures.mcp_workspace import (  # noqa: E402
    in_workspace_project,
    write_run,
)
from tests.fixtures.stdio import initialized_stdio_session  # noqa: E402


def _ev(seq: int, kind: str = "phase.start", phase: str = "plan", **payload):
    return {
        "seq": seq,
        "ts": f"2026-01-01T00:00:{seq:02d}",
        "kind": kind,
        "phase": phase,
        "payload": payload,
    }


@pytest.mark.anyio
async def test_stdio_events_summary_call_tool(fake_workspace):
    """``orcho_run_events_summary`` round-trips correctly over stdio.

    Seeds a deterministic fake run, spawns ``python -m orcho_mcp`` with
    ``ORCHO_WORKSPACE`` pointing at the fixture, calls the tool, and
    asserts the structured response parses as ``RunEventsSummary``.
    """
    # Seed a 10-event run with two phases so the aggregator has something
    # meaningful to summarise.
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p", "status": "running", "task": "stdio smoke"},
        events=[_ev(i) for i in range(1, 11)],
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_run_events_summary",
            {"run_id": "20260101_000001"},
        )
        # FastMCP returns a structured tool result; ``structuredContent``
        # carries the dict the Pydantic model serialised to.
        payload = result.structuredContent
        assert payload is not None, "tool result must carry structuredContent"
        assert payload["run_id"] == "20260101_000001"
        assert payload["total_count"] == 10
        assert payload["next_seq"] == 10
        assert payload["eof"] is True
        assert "by_kind" in payload
        assert "by_phase" in payload
        assert isinstance(payload["next_actions"], list)
        # Bounded: tail capped at the default last_n=5.
        assert len(payload["last_n"]) == 5


@pytest.mark.anyio
async def test_stdio_watch_call_tool(fake_workspace):
    """``orcho_run_watch`` round-trips correctly over stdio.

    Seeds a small event stream and calls watch with ``until=next_event``
    so the tool returns via the fast-path without sleeping. Asserts the
    structured response parses as ``RunWatchResult`` with a populated
    bounded summary. progressToken capture lives in the L4 integration
    test — this smoke just proves stdio framing + schema serialisation.
    """
    write_run(
        fake_workspace, "20260101_000001",
        meta={"project": "/p", "status": "running", "task": "watch smoke"},
        events=[_ev(i) for i in range(1, 6)],
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_run_watch",
            {
                "run_id": "20260101_000001",
                "since_seq": 0,
                "until": "next_event",
                "timeout_s": 5,
            },
        )
        payload = result.structuredContent
        assert payload is not None, "tool result must carry structuredContent"
        assert payload["run_id"] == "20260101_000001"
        assert payload["triggered"] is True
        assert payload["trigger"]["kind"] == "next_event"
        assert payload["trigger"]["seq"] == 5
        assert payload["summary"] is not None
        assert payload["summary"]["next_seq"] == 5
        # handoff field is absent / None for non-handoff triggers.
        assert payload.get("handoff") is None


@pytest.mark.anyio
async def test_stdio_watch_handoff_shape(fake_workspace):
    """Enriched handoff round-trips correctly over stdio.

    Seeds a paused run with a ``phase_handoff`` block carrying actions
    and findings, calls ``orcho_run_watch`` over a real stdio session,
    and asserts the structured response includes ``client_hints``,
    ``default_action``, and the compact findings list. Proves the new
    Pydantic models serialise across the JSON-RPC boundary.
    """
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p",
            "status": "awaiting_phase_handoff",
            "task": "handoff smoke",
            # Backward-compat subset: a runtime that offers only the
            # legacy three verbs. The all-four wire surface (incl.
            # ``continue_with_waiver``) is proven by
            # ``test_stdio_watch_handoff_waiver_wire_surface`` below.
            "phase_handoff": {
                "id": "validate_plan:plan_round:2",
                "phase": "validate_plan",
                "available_actions": ["retry_feedback", "continue", "halt"],
                "findings": [
                    {"severity": "P1", "title": "Missing stdio smoke",
                     "required_fix": "Add L3 test"},
                ],
            },
        },
        events=[_ev(1), _ev(2)],
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_run_watch",
            {
                "run_id": "20260101_000001",
                "since_seq": 0,
                "until": "handoff_or_terminal",
                "timeout_s": 5,
            },
        )
        payload = result.structuredContent
        assert payload is not None
        assert payload["trigger"]["kind"] == "handoff"
        handoff = payload["handoff"]
        assert handoff is not None
        assert handoff["run_id"] == "20260101_000001"
        assert handoff["default_action"] == "retry_feedback"
        assert handoff["feedback_required_for"] == ["retry_feedback"]
        assert handoff["available_actions"] == ["retry_feedback", "continue", "halt"]
        assert len(handoff["findings"]) == 1
        assert handoff["findings"][0]["severity"] == "P1"
        # Client hints serialise as a nested object. Without an explicit
        # ``interaction_client``, the response carries the generic profile
        # Generic profile: free-form, chat render.
        hints = handoff["client_hints"]
        assert hints["client"] == "generic"
        assert hints["interaction_style"] == "free_form"
        assert hints["preferred_render"] == "chat"
        assert hints["clarify_on_ambiguous_reply"] is True
        assert hints["action_field"] == "action"
        assert hints["feedback_field"] == "feedback"


@pytest.mark.anyio
async def test_stdio_watch_handoff_waiver_wire_surface(fake_workspace):
    """The four-action handoff surface round-trips over stdio.

    Seeds a rejected handoff whose ``available_actions`` includes
    ``continue_with_waiver`` and asserts the MCP-speaking client sees the
    new verb end-to-end: it rides in ``available_actions``, joins
    ``feedback_required_for``, surfaces as a feedback-gated choice with
    native elicitation metadata, and carries the non-terminal
    ``orcho_run_resume`` followup. Guards against a regression where the
    structured content drops the waiver verb or its feedback gating.
    """
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p",
            "status": "awaiting_phase_handoff",
            "task": "waiver wire smoke",
            "phase_handoff": {
                "id": "validate_plan:plan_round:2",
                "phase": "validate_plan",
                "available_actions": [
                    "continue", "retry_feedback", "continue_with_waiver", "halt",
                ],
                "findings": [
                    {"severity": "P1", "title": "Missing stdio smoke"},
                ],
            },
        },
        events=[_ev(1), _ev(2)],
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_run_watch",
            {
                "run_id": "20260101_000001",
                "since_seq": 0,
                "until": "handoff_or_terminal",
                "timeout_s": 5,
            },
        )
        payload = result.structuredContent
        assert payload is not None
        assert payload["trigger"]["kind"] == "handoff"
        handoff = payload["handoff"]
        assert handoff is not None
        assert handoff["available_actions"] == [
            "continue", "retry_feedback", "continue_with_waiver", "halt",
        ]
        assert "continue_with_waiver" in handoff["feedback_required_for"]

        choices = {c["action"]: c for c in handoff["choices"]}
        assert "continue_with_waiver" in choices, (
            "continue_with_waiver must serialise as a decision choice"
        )
        waiver = choices["continue_with_waiver"]
        assert waiver["requires_feedback"] is True
        assert waiver["feedback_field"] == "feedback"
        assert waiver["elicitation"] is not None
        assert waiver["elicitation"]["mode"] == "form"
        assert waiver["elicitation"]["client_capability"] == "elicitation.form"
        # Feedback is never pre-filled into args over the wire.
        assert "feedback" not in waiver["args"]
        assert waiver["args"]["action"] == "continue_with_waiver"
        # Non-terminal verb: resume followup rides through structuredContent.
        assert waiver["followup"] is not None
        assert waiver["followup"]["tool"] == "orcho_run_resume"
        assert waiver["followup"]["args"] == {"run_id": "20260101_000001"}


@pytest.mark.anyio
async def test_stdio_watch_codex_profile(fake_workspace):
    """``interaction_client="codex"`` round-trips through stdio
    and shapes ``client_hints`` toward the Ask-style render.

    Proves the new ``interaction_client`` parameter is wired into the
    schema, accepted by FastMCP, and reaches ``_build_handoff_hint``
    end-to-end. Correctness fields (``available_actions``,
    ``default_action``) remain unchanged across profiles — asserted
    here too as a cross-check against the L1 invariance test.
    """
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p",
            "status": "awaiting_phase_handoff",
            "task": "codex profile smoke",
            "phase_handoff": {
                "id": "validate_plan:plan_round:2",
                "phase": "validate_plan",
                "available_actions": ["retry_feedback", "continue", "halt"],
                "findings": [
                    {"severity": "P1", "title": "Missing stdio smoke"},
                ],
            },
        },
        events=[_ev(1)],
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_run_watch",
            {
                "run_id": "20260101_000001",
                "since_seq": 0,
                "until": "handoff_or_terminal",
                "timeout_s": 5,
                "interaction_client": "codex",
            },
        )
        payload = result.structuredContent
        assert payload is not None
        handoff = payload["handoff"]
        assert handoff is not None
        hints = handoff["client_hints"]
        assert hints["client"] == "codex"
        assert hints["interaction_style"] == "ask"
        assert hints["preferred_render"] == "ask"
        assert hints["show_actions"] is True
        assert hints["allow_feedback_text"] is True
        assert hints["include_followup_tools"] is True
        # Profile does not affect correctness fields.
        assert handoff["available_actions"] == [
            "retry_feedback", "continue", "halt",
        ]
        assert handoff["default_action"] == "retry_feedback"


@pytest.mark.anyio
async def test_stdio_diagnose_needs_decision_call_tool(fake_workspace):
    """``orcho_run_diagnose`` round-trips a typed ``RunDiagnosis`` over stdio.

    Seeds a run paused on ``awaiting_phase_handoff`` and calls the tool
    through a real ``python -m orcho_mcp`` subprocess. Asserts the structured
    response parses as ``RunDiagnosis`` (no stdout pollution / framing break),
    classifies as ``needs_decision``, and that the phase-handoff decide
    ``next_actions`` carry the typed call-readiness contract: the
    feedback-gated verbs are ``operator_input_required`` (never a directly
    callable ``ready_call``) and never pre-fill the operator's ``feedback``.
    """
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p",
            "status": "awaiting_phase_handoff",
            "task": "diagnose needs_decision smoke",
            "phase_handoff": {
                "id": "validate_plan:plan_round:2",
                "phase": "validate_plan",
                "available_actions": [
                    "continue", "retry_feedback", "halt", "continue_with_waiver",
                ],
            },
        },
        events=[_ev(1), _ev(2)],
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_run_diagnose",
            {"run_id": "20260101_000001"},
        )

    # A clean structured result over stdio proves JSON-RPC framing stayed
    # intact (any stdout pollution breaks the frame and fails call_tool).
    assert result.isError is False
    payload = result.structuredContent
    assert payload is not None, "tool result must carry structuredContent"

    # Parses as the typed wire model end-to-end.
    from orcho_mcp.schemas import RunDiagnosis

    diag = RunDiagnosis.model_validate(payload)
    assert diag.run_id == "20260101_000001"
    assert diag.condition == "needs_decision"
    assert diag.available_actions == [
        "continue", "retry_feedback", "halt", "continue_with_waiver",
    ]

    decides = [
        na for na in payload["next_actions"]
        if na["tool"] == "orcho_phase_handoff_decide"
    ]
    assert decides, "needs_decision must surface decide next_actions"

    # No decide record is a ready_call without a valid substituted action.
    for na in decides:
        if na["kind"] == "ready_call":
            assert na["args"].get("action") in ("continue", "halt")

    # The feedback-gated verbs are operator_input_required — checked via the
    # typed ``kind`` field, not intent prose — carrying choices + the feedback
    # input_schema, and never pre-filling the operator's ``feedback``.
    oir = [na for na in decides if na["kind"] == "operator_input_required"]
    assert {na["args"]["action"] for na in oir} == {
        "retry_feedback", "continue_with_waiver",
    }
    for na in oir:
        assert na["requires_operator_input"] is True
        assert na["choices"]
        assert na["input_schema"] and "feedback" in na["input_schema"]["properties"]
        assert "feedback" not in na["args"], (
            "operator feedback must never be pre-substituted into args"
        )


@pytest.mark.anyio
async def test_stdio_delivery_gate_correction_call_tool(fake_workspace):
    """``orcho_delivery_gate`` round-trips a typed projection over stdio.

    Seeds a run defer-parked at a rejected correction gate
    (``commit_delivery.status == 'pending'`` + a REJECTED release) and calls the
    tool through a real ``python -m orcho_mcp`` subprocess. Asserts the
    structured response parses as ``DeliveryGateProjection``, classifies as
    ``correction_decision_required``, and that ``next_actions`` carry ready
    ``orcho_delivery_decide`` calls for SDK-available actions. (The post-fix
    follow-up state is covered by the L1 delivery-gate unit tests.)
    """
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/repo/checkout",
            "status": "halted",
            "halt_reason": "commit_delivery_pending",
            "task": "delivery gate stdio smoke",
            "commit_delivery": {
                "status": "pending",
                "action": "fix",
                "release_verdict": "REJECTED",
                "project_path": "/repo/checkout",
                "source_path": "/repo/worktree",
                "changed_paths": ["src/a.py"],
                "untracked_paths": [],
            },
        },
        commit_decision={
            "action": "fix",
            "commit_status": "pending",
            "files_staged": ["src/a.py"],
        },
        diff_patch=(
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        ),
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_delivery_gate",
            {"run_id": "20260101_000001"},
        )

    assert result.isError is False
    payload = result.structuredContent
    assert payload is not None, "tool result must carry structuredContent"

    from orcho_mcp.schemas import DeliveryGateProjection

    proj = DeliveryGateProjection.model_validate(payload)
    assert proj.run_id == "20260101_000001"
    assert proj.kind == "correction_decision_required"
    assert proj.release == "rejected"
    assert proj.diff.degraded is False
    assert proj.diff.changed_paths == ["src/a.py"]
    assert [a.action for a in proj.available_actions] == ["fix", "halt"]
    assert proj.blocked_actions == ["approve", "apply", "skip"]
    assert proj.next_actions
    assert [na.args["action"] for na in proj.next_actions] == [
        "fix", "halt",
    ]
    for na in proj.next_actions:
        assert na.kind == "ready_call"
        assert na.requires_operator_input is False
        assert na.tool == "orcho_delivery_decide"
        assert na.args["run_id"] == "20260101_000001"


@pytest.mark.anyio
async def test_stdio_workspace_state_after_summary(fake_workspace):
    """``orcho_run_events_summary`` updates the advisory MCP
    workspace state, and ``orcho_workspace_state`` exposes the same
    record over stdio.

    Proves the state file is written from inside the spawned server
    process (not just in-process unit tests) and the round-trip
    serialisation works end-to-end.
    """
    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": "/p", "status": "running",
            "task": "state stdio smoke",
        },
        events=[_ev(i) for i in range(1, 8)],
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        summary_res = await session.call_tool(
            "orcho_run_events_summary",
            {"run_id": "20260101_000001"},
        )
        sp = summary_res.structuredContent
        assert sp is not None
        assert sp["next_seq"] == 7

        state_res = await session.call_tool(
            "orcho_workspace_state", {},
        )
        state = state_res.structuredContent
        assert state is not None
        assert state["version"] == 1
        assert "20260101_000001" in state["runs"]
        record = state["runs"]["20260101_000001"]
        assert record["last_seq"] == 7
        assert record["last_status"] == "running"
        assert record["last_summary_at"]
        # No PII / payload sneaking through the wire.
        banned = {"events", "payload", "findings", "prompt", "env", "secrets"}
        assert set(record.keys()).isdisjoint(banned)


@pytest.mark.anyio
async def test_stdio_workflows_list_call_tool(fake_workspace):
    """``orcho_workflows_list`` round-trips correctly over stdio.

    Spawns the server, calls the tool, and asserts the structured
    response carries the canonical workflow-recipe catalogue. This
    is the tools-only delivery channel; resource-aware clients read
    the same payload via ``orcho://workflows``.
    """
    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool("orcho_workflows_list", {})
        payload = result.structuredContent
        assert payload is not None, "tool result must carry structuredContent"
        assert payload["format_version"] == 2
        names = [recipe["name"] for recipe in payload["recipes"]]
        assert names == [
            "plan_then_implement",
            "review_paused_run",
            "resume_failed_run",
            "inspect_terminal_run",
            "diagnose_halted_run",
            "observe_active_run",
            "inspect_delivery_gate",
        ]
        expected_recipe_versions = {
            "plan_then_implement": 2,
            "review_paused_run": 2,
            "resume_failed_run": 2,
            "inspect_terminal_run": 1,
            "diagnose_halted_run": 2,
            "observe_active_run": 2,
            "inspect_delivery_gate": 1,
        }
        for recipe in payload["recipes"]:
            assert recipe["format_version"] == expected_recipe_versions[recipe["name"]]
            assert isinstance(recipe["steps"], list)
            assert recipe["steps"], (
                f"{recipe['name']}: empty step list serialised over stdio"
            )
        plan_recipe = next(
            recipe for recipe in payload["recipes"]
            if recipe["name"] == "plan_then_implement"
        )
        resource_steps = [
            step for step in plan_recipe["steps"]
            if step["kind"] == "resource"
        ]
        assert resource_steps
        assert all(step["uri"].endswith("/summary") for step in resource_steps)
        # The resilient observation recipe survives stdio serialisation with
        # its bounded watch + events_summary fallback intact.
        observe_recipe = next(
            recipe for recipe in payload["recipes"]
            if recipe["name"] == "observe_active_run"
        )
        observe_tools = [
            step for step in observe_recipe["steps"]
            if step["kind"] == "tool"
        ]
        bounded_watch = next(
            step for step in observe_tools
            if step["tool"] == "orcho_run_watch"
        )
        assert 120 <= int(bounded_watch["args"]["timeout_s"]) <= 240
        assert any(
            step["tool"] == "orcho_run_events_summary" for step in observe_tools
        )


@pytest.mark.anyio
async def test_stdio_run_start_schema_documents_auto_detect(fake_workspace):
    """``orcho_run_start`` advertises ``profile="auto-detect"`` over the wire.

    The selector is part of the public tool contract, so the listed tool
    description (what an LLM client reads to learn the surface) must mention
    it. Pins T4's docstring change against the serialised ``list_tools``
    catalogue.
    """
    async with initialized_stdio_session(fake_workspace) as (session, _):
        tools = await session.list_tools()
        start = next(t for t in tools.tools if t.name == "orcho_run_start")
        assert "auto-detect" in (start.description or ""), (
            "orcho_run_start description must document the auto-detect selector"
        )


@pytest.mark.anyio
async def test_stdio_run_status_exposes_auto_detect_projection(fake_workspace):
    """``orcho_run_status`` round-trips the typed ``auto_detect`` projection.

    Seeds a run whose ``meta.auto_detect`` records a low-confidence fallback
    (a non-trivial disposition), then asserts the stdio payload carries the
    ``auto_detect`` block with ``requested_selector`` and a deterministic
    ``next_action`` that never emits an empty ``args.profile``.
    """
    write_run(
        fake_workspace, "20260101_000050",
        meta={
            "project": "/p", "status": "done", "task": "auto-detect smoke",
            "auto_detect": {
                "detection_state": "low_confidence_fallback",
                "actual_profile": "feature",
                "actual_mode": "fast",
                "recommended_profile": "complex_feature",
                "recommended_mode": "fast",
                "policy": "trust_above_threshold",
                "confidence": 0.21,
                "fallback_used": True,
                "confirmation_state": None,
                "risk_flags": [],
                "rationale": None,
                "error_reason": None,
                "fallback_reason": "confidence 0.21 < threshold 0.7",
            },
        },
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_run_status", {"run_id": "20260101_000050"},
        )
        payload = result.structuredContent
        assert payload is not None, "tool result must carry structuredContent"
        ad = payload["auto_detect"]
        assert ad is not None, "auto_detect projection missing from RunStatus"
        assert ad["requested_selector"] == "auto-detect"
        assert ad["detection_state"] == "low_confidence_fallback"
        assert ad["selected_profile"] == "feature"
        assert ad["trusted"] is False
        na = ad["next_action"]
        assert na is not None, "fallback disposition must carry a next_action"
        assert na["kind"] == "operator_input_required"
        # Invariant: a concrete candidate yields a NON-empty args.profile.
        assert na["args"]["profile"] == "feature"


@pytest.mark.anyio
async def test_stdio_run_status_auto_detect_absent_for_manual_run(fake_workspace):
    """A run that did not use the selector projects ``auto_detect=None``."""
    write_run(
        fake_workspace, "20260101_000051",
        meta={"project": "/p", "status": "done", "task": "manual run"},
    )
    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_run_status", {"run_id": "20260101_000051"},
        )
        payload = result.structuredContent
        assert payload is not None
        assert payload["auto_detect"] is None


@pytest.mark.anyio
async def test_stdio_profiles_list_exposes_selectors(fake_workspace):
    """``orcho_profiles_list`` round-trips the ``selectors`` field over stdio.

    ``auto-detect`` is surfaced as a selector (disjoint from executable
    ``profiles``) regardless of whether the v2 catalogue is present, since
    selectors do not depend on that file.
    """
    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool("orcho_profiles_list", {})
        payload = result.structuredContent
        assert payload is not None, "tool result must carry structuredContent"
        selectors = payload["selectors"]
        selector_names = {s["name"] for s in selectors}
        assert "auto-detect" in selector_names
        auto = next(s for s in selectors if s["name"] == "auto-detect")
        assert auto["is_selector"] is True
        # Disjoint from executable profiles.
        profile_names = {p["name"] for p in payload["profiles"]}
        assert "auto-detect" not in profile_names


def _write_advice_artifact(run_dir, name, *, handoff_id, usage=None):
    """Seed one durable Stage 0/1 advice artifact under the run dir.

    Mirrors the shape orcho-core's advisor writes (and the SDK projection
    reads). Hermetic: no advisor / LLM is invoked — the evidence slice reads
    these durable JSON files.
    """
    import json

    advice_dir = run_dir / "phase_handoff_advice"
    advice_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_dir.name,
        "handoff_id": handoff_id,
        "phase": "review_changes",
        "created_at": "2026-01-01T00:00:00+00:00",
        "advice": {
            "recommended_action": "retry_feedback",
            "confidence": "high",
            "rationale": "bounded fix",
            "retry_feedback": "close the named gap",
            "risks": [],
            "expected_files": [],
            "operator_note": "",
            "parse_warnings": [],
        },
        "raw_output": "",
        "usage": usage or {},
    }
    (advice_dir / name).write_text(json.dumps(payload), encoding="utf-8")
    return f"phase_handoff_advice/{name}"


def _write_advice_decision(run_dir, name, *, advice_relpath, handoff_id):
    """Seed a decision artifact whose note links it to the advice artifact."""
    import json

    decisions_dir = run_dir / "phase_handoff_decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    note = f"feedback_source=agent_advice; advice_artifact={advice_relpath}"
    payload = {
        "run_id": run_dir.name,
        "handoff_id": handoff_id,
        "phase": "review_changes",
        "action": "retry_feedback",
        "feedback": "close the named gap",
        "note": note,
        "decided_at": "2026-01-01T00:05:00+00:00",
    }
    (decisions_dir / name).write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.anyio
async def test_stdio_run_evidence_handoff_advice_slice(fake_workspace):
    """``orcho_run_evidence(slice="handoff_advice")`` round-trips over stdio.

    Seeds a terminal run with durable Stage 0/1 advice + decision artifacts and
    a review-round verdict, then calls the tool through a real ``python -m
    orcho_mcp`` subprocess. Hermetic: the slice reads the SDK projection over
    durable artifacts — no advisor / LLM is invoked. Asserts the new wire fields
    serialise end-to-end: per-call (handoff_id / recommended_action /
    applied_action / confidence / resolved / outcome / advice_artifact / usage)
    and the aggregate summary (calls / applied_retries / resolved_retries).
    """
    handoff_id = "review_changes:repair_round:1"
    run_dir = write_run(
        fake_workspace, "20260101_000010",
        meta={
            "project": "/p", "status": "done", "task": "advice evidence smoke",
            # A review round whose blank-vs-nonblank critique drives the
            # resolved/repeated classifier; the run reached terminal ``done``.
            "phases": {"rounds": [{"round": 1, "critique": "P1: broken"}]},
        },
        events=[_ev(1, kind="run.start"), _ev(2, kind="run.end")],
    )
    rel = _write_advice_artifact(
        run_dir, "h1.json", handoff_id=handoff_id,
        usage={"tokens_in": 100, "tokens_out": 50},
    )
    _write_advice_decision(
        run_dir, "d1.json", advice_relpath=rel, handoff_id=handoff_id,
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_run_evidence",
            {"run_id": "20260101_000010", "slice": "handoff_advice"},
        )

    assert result.isError is False
    payload = result.structuredContent
    assert payload is not None, "tool result must carry structuredContent"

    from orcho_mcp.schemas import EvidenceResult

    ev = EvidenceResult.model_validate(payload)
    assert ev.run_id == "20260101_000010"
    assert ev.slice == "handoff_advice"
    assert ev.handoff_advice is not None
    assert len(ev.handoff_advice.calls) == 1
    call = ev.handoff_advice.calls[0]
    assert call.handoff_id == handoff_id
    assert call.phase == "review_changes"
    assert call.advice_artifact == "phase_handoff_advice/h1.json"
    assert call.recommended_action == "retry_feedback"
    assert call.applied_action == "retry_feedback"
    assert call.confidence == "high"
    assert call.resolved is True
    assert call.outcome == "resolved"
    assert call.tokens_in == 100
    assert call.tokens_out == 50

    summary = ev.handoff_advice.summary
    assert summary.calls == 1
    assert summary.applied_retries == 1
    assert summary.resolved_retries == 1


@pytest.mark.anyio
async def test_stdio_run_evidence_handoff_advice_empty_when_absent(fake_workspace):
    """The ``handoff_advice`` slice degrades to a clean empty record over stdio.

    A run with no Stage 0/1 advisor surface must serialise an empty slice
    (``calls=[]`` + zeroed summary), never a transport error.
    """
    write_run(
        fake_workspace, "20260101_000011",
        meta={"project": "/p", "status": "done", "task": "no advice surface"},
        events=[_ev(1, kind="run.start")],
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_run_evidence",
            {"run_id": "20260101_000011", "slice": "handoff_advice"},
        )

    assert result.isError is False
    payload = result.structuredContent
    assert payload is not None
    assert payload["handoff_advice"] is not None
    assert payload["handoff_advice"]["calls"] == []
    assert payload["handoff_advice"]["summary"]["calls"] == 0


@pytest.mark.anyio
async def test_stdio_handoff_advice_tool_typed_error(fake_workspace):
    """``orcho_handoff_advice`` is registered and maps SDK errors over stdio.

    A terminal (not-paused) run is ineligible for advice: the SDK accessor
    raises ``InvalidPhaseHandoffState`` BEFORE any advisor invocation, which the
    boundary maps to ``InvalidPlanError``. Calling the tool over a real ``python
    -m orcho_mcp`` subprocess proves it is registered, wired end-to-end, and
    surfaces a typed error (``isError``) without stdout pollution — all hermetic
    (no advisor / LLM call on this path).

    The advisor SUCCESS path (and its ``ready_next_action.args.note`` provenance)
    is exercised hermetically in
    ``test_stdio_handoff_advice_tool_success_path``: a ``mock=True`` supervisor
    state makes the MCP boundary recover a ``MockAgentProvider`` so the advisor
    runs deterministically with zero real-provider calls.
    """
    write_run(
        fake_workspace, "20260101_000012",
        meta={"project": "/p", "status": "done", "task": "not paused for advice"},
        events=[_ev(1, kind="run.start"), _ev(2, kind="run.end")],
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        # First prove the tool is in the advertised catalogue over the wire.
        tools = await session.list_tools()
        assert "orcho_handoff_advice" in {t.name for t in tools.tools}, (
            "orcho_handoff_advice must be registered in the stdio catalogue"
        )

        result = await session.call_tool(
            "orcho_handoff_advice", {"run_id": "20260101_000012"},
        )

    # A clean typed error round-trips: the frame stayed intact (any stdout
    # pollution would break call_tool itself) and the not-paused run is refused.
    assert result.isError is True
    text = " ".join(
        getattr(block, "text", "") for block in (result.content or [])
    ).lower()
    assert "advice" in text or "handoff" in text or "paused" in text


_ELIGIBLE_HANDOFF_ID = "review_changes:review:2"


def _eligible_phase_handoff() -> dict:
    """An advice-eligible paused phase-handoff payload (rejected verdict).

    Mirrors the shape orcho-core's ``request_handoff_advice`` SDK contract test
    uses: a rejected trigger with a rejected-equivalent verdict, ``retry_feedback``
    offered, and a finding present — the predicate ``advice_actions_available``
    accepts it, so the advisor actually runs.
    """
    return {
        "id": _ELIGIBLE_HANDOFF_ID,
        "phase": "review_changes",
        "type": "human_feedback_on_reject",
        "trigger": "rejected",
        "verdict": "REJECTED",
        "approved": False,
        "round_extras_key": "review",
        "round": 2,
        "loop_max_rounds": 2,
        "available_actions": [
            "continue", "retry_feedback", "halt", "continue_with_waiver",
        ],
        "artifacts": {
            "findings": [
                {"id": "F1", "severity": "P1", "title": "bug", "body": "fix it"},
            ],
        },
        "last_output": "reviewer rejected the change",
    }


@pytest.mark.anyio
async def test_stdio_handoff_advice_tool_success_path(fake_workspace):
    """``orcho_handoff_advice`` serialises a typed success payload over stdio.

    Hermetic success path: a ``mock=True`` supervisor state makes the MCP
    boundary recover a ``MockAgentProvider`` (see
    ``orcho_mcp.run_control.advice._resolve_advisor_provider``), so the advisor
    runs deterministically with ZERO real-provider calls. Seeds a paused,
    advice-eligible rejected handoff and calls the tool over a real ``python -m
    orcho_mcp`` subprocess, asserting the wire-serialised ``structuredContent``:
    the recommendation, safety, and — the acceptance criterion — the
    ``ready_next_action`` pre-filled ``orcho_phase_handoff_decide`` retry call
    whose ``args.note`` carries the non-empty provenance note.

    This is the L3 counterpart to the L1 monkeypatch test in
    ``tests/unit/run_control/test_advice.py``; it is what catches wire / schema /
    serialization regressions the in-process test cannot see.
    """
    # The advisor rebuilds a read-only run from the project dir, so it must
    # exist on disk for the subprocess. A plain directory is sufficient (the
    # advice path does not require a git checkout — see the orcho-core SDK test).
    project_dir = fake_workspace / "proj"
    project_dir.mkdir(parents=True, exist_ok=True)

    write_run(
        fake_workspace, "20260101_000013",
        meta={
            "project": str(project_dir),
            "model": "claude-opus-4-8",
            "profile": "feature",
            "status": "awaiting_phase_handoff",
            "task": "fix the rejected change",
            "phase_handoff": _eligible_phase_handoff(),
            "phases": {},
        },
        events=[_ev(1, kind="run.start")],
        # mock=True is the seam: the MCP advisor path resolves a MockAgentProvider
        # for this run, keeping the in-process advisor call hermetic.
        supervisor_state={
            "run_id": "20260101_000013",
            "status": "awaiting_phase_handoff",
            "project_dir": str(project_dir),
            "mock": True,
        },
    )

    async with initialized_stdio_session(fake_workspace) as (session, _):
        result = await session.call_tool(
            "orcho_handoff_advice", {"run_id": "20260101_000013"},
        )

    assert result.isError is False, (
        "advice success path must not error under mock provider; "
        f"content={result.content}"
    )
    payload = result.structuredContent
    assert payload is not None, "tool result must carry structuredContent"

    from orcho_mcp.schemas import HandoffAdviceResult

    advice = HandoffAdviceResult.model_validate(payload)
    assert advice.run_id == "20260101_000013"
    assert advice.handoff_id == _ELIGIBLE_HANDOFF_ID
    assert advice.phase == "review_changes"
    # The mock advisor recommends a confident retry with concrete feedback.
    assert advice.recommended_action == "retry_feedback"
    assert advice.confidence == "high"
    assert advice.retry_feedback
    assert advice.safety is not None
    assert advice.advice_artifact.startswith("phase_handoff_advice/")
    assert advice.provenance_note

    # The acceptance criterion: ready_next_action is a pre-filled call to the
    # EXISTING decide verb carrying mandatory provenance — not a new verb.
    ready = advice.ready_next_action
    assert ready is not None
    assert ready.tool == "orcho_phase_handoff_decide"
    assert ready.args["action"] == "retry_feedback"
    assert ready.args["feedback"] == advice.retry_feedback
    assert ready.args["note"] == advice.provenance_note
    assert ready.args["note"], "ready_next_action.args.note must be non-empty"


@pytest.mark.anyio
async def test_stdio_workspace_pending_decisions_call_tool(fake_workspace):
    """``orcho_workspace_pending_decisions`` round-trips over stdio.

    Seeds two *actionable* paused runs (project under the workspace root, so
    they survive the default noise filter) — one with no recorded decision,
    one whose decision artifact already exists (recorded in-process via the
    SDK so the subprocess reads it off disk) — a running run, and a paused run
    with a missing project (hidden by default). Asserts the tool is in the
    advertised stdio catalogue and that ``call_tool`` returns a typed
    ``WorkspacePendingDecisionsResult`` over the wire: the default view shows
    only the two actionable runs with the decide→resume branch on
    ``decision_artifact_exists`` and a ``hidden_count`` for the filtered run,
    while ``include_stale=True`` round-trips as an argument and returns the
    hidden run carrying its real ``classification`` with identical counters —
    and no raw findings / reviewer body ever leaks.
    """
    from sdk import phase_handoff_decide

    proj = in_workspace_project(fake_workspace)

    write_run(
        fake_workspace, "20260101_000001",
        meta={
            "project": proj,
            "status": "awaiting_phase_handoff",
            "task": "pending decisions smoke — undecided",
            "phase_handoff": {
                "id": "validate_plan:plan_round:1",
                "phase": "validate_plan",
                "available_actions": ["continue", "retry_feedback", "halt"],
                # A raw reviewer body that must NOT leak onto the row.
                "last_output": "X" * 2000,
                "findings": [{"severity": "P1", "title": "secret body"}],
            },
        },
        events=[_ev(1)],
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta={
            "project": proj,
            "status": "awaiting_phase_handoff",
            "task": "pending decisions smoke — decided",
            "phase_handoff": {
                "id": "validate_plan:plan_round:2",
                "phase": "validate_plan",
                "available_actions": ["continue", "retry_feedback", "halt"],
            },
        },
        events=[_ev(1)],
    )
    # ``continue`` records a decision artifact without flipping status off
    # paused — the row must now route to resume, not a second decide.
    phase_handoff_decide(
        "20260101_000002", "validate_plan:plan_round:2", "continue", cwd=None,
    )
    write_run(
        fake_workspace, "20260101_000003",
        meta={"project": proj, "status": "running", "task": "still running"},
        events=[_ev(1)],
    )
    # A paused run with a missing project → hidden by default, forensic-only.
    write_run(
        fake_workspace, "20260101_000000",
        meta={
            "project": "/p/does-not-exist",
            "status": "awaiting_phase_handoff",
            "task": "pending decisions smoke — missing project",
            "phase_handoff": {
                "id": "validate_plan:plan_round:0",
                "phase": "validate_plan",
                "available_actions": ["continue", "halt"],
            },
        },
        events=[_ev(1)],
    )

    from orcho_mcp.schemas import WorkspacePendingDecisionsResult

    async with initialized_stdio_session(fake_workspace) as (session, _):
        tools = await session.list_tools()
        assert "orcho_workspace_pending_decisions" in {
            t.name for t in tools.tools
        }, "tool must be registered in the stdio catalogue"

        default_result = await session.call_tool(
            "orcho_workspace_pending_decisions", {},
        )
        # The new ``include_stale`` argument round-trips over the wire.
        forensic_result = await session.call_tool(
            "orcho_workspace_pending_decisions", {"include_stale": True},
        )

    assert default_result.isError is False
    payload = default_result.structuredContent
    assert payload is not None, "tool result must carry structuredContent"

    res = WorkspacePendingDecisionsResult.model_validate(payload)
    # Only the two actionable paused runs surface, newest id first; the
    # running run and the missing-project run are out of the default view.
    assert [r.run_id for r in res.runs] == ["20260101_000002", "20260101_000001"]
    assert res.returned_count == 2
    assert res.truncated is False
    assert all(r.classification == "actionable" for r in res.runs)
    # The missing-project run is hidden but counted in the breakdown.
    assert res.hidden_count == 1
    assert res.hidden_missing_project_count == 1
    assert res.hidden_temp_project_count == 0
    assert res.hidden_out_of_workspace_count == 0

    by_id = {r.run_id: r for r in res.runs}
    # Undecided run → operator_input_required decide with the available verbs.
    undecided = by_id["20260101_000001"]
    assert undecided.decision_artifact_exists is False
    [na] = undecided.next_actions
    assert na.tool == "orcho_phase_handoff_decide"
    assert na.kind == "operator_input_required"
    assert na.choices == ["continue", "retry_feedback", "halt"]
    # Decided run → ready resume, never a second decide.
    decided = by_id["20260101_000002"]
    assert decided.decision_artifact_exists is True
    [na2] = decided.next_actions
    assert na2.tool == "orcho_run_resume"
    assert na2.kind == "ready_call"
    assert na2.args == {"run_id": "20260101_000002"}

    # Forensic view: the hidden missing-project run is returned with its real
    # classification, and the counters are identical to the default call.
    assert forensic_result.isError is False
    forensic = WorkspacePendingDecisionsResult.model_validate(
        forensic_result.structuredContent,
    )
    assert [r.run_id for r in forensic.runs] == [
        "20260101_000002", "20260101_000001", "20260101_000000",
    ]
    forensic_by_id = {r.run_id: r for r in forensic.runs}
    assert forensic_by_id["20260101_000000"].classification == "missing_project"
    assert forensic_by_id["20260101_000002"].classification == "actionable"
    assert forensic.hidden_count == res.hidden_count == 1
    assert (
        forensic.hidden_missing_project_count
        == res.hidden_missing_project_count
        == 1
    )

    # No raw reviewer body / findings leak across the wire in either view.
    assert "secret body" not in str(default_result.structuredContent)
    assert "X" * 100 not in str(default_result.structuredContent)
    assert "secret body" not in str(forensic_result.structuredContent)


@pytest.fixture
def anyio_backend():
    # Pin to asyncio — pytest-anyio defaults try trio too, which we don't
    # ship as a dep.
    return "asyncio"
