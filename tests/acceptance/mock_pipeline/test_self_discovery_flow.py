"""Self-discovery flow — agent dogfood over a real stdio MCP session.

End-to-end smoke that walks a cold MCP client through the
self-discovery surfaces:

  1. ``orcho_workspace_info`` — instance discovery.
  2. ``orcho_workflows_list`` — machine-readable recipe catalogue.
  3. ``orcho://workflows`` — same catalogue via the resource channel.
  4. ``orcho_run_start`` — spawn a mock run that pauses on
     ``validate_plan`` so a handoff is guaranteed.
  5. ``orcho_run_watch(until="handoff_or_terminal")`` — observe.
  6. ``orcho_run_status`` — read ``artefacts`` and confirm the URIs
     resolve through ``read_resource``.
  7. ``orcho_phase_handoff_decide`` — call ``retry_feedback`` without
     prefilled feedback from an elicitation-capable client and confirm
     the server collects the missing field natively.

The load-bearing assertion is the self-discovery contract:

  * ``RunStatus.artefacts`` is populated when artefacts exist on disk;
  * every advertised URI resolves through the MCP resource channel;
  * the handoff's ``choices`` carry only known actions, never leak a
    placeholder feedback string into ``args``, and decorate
    ``retry_feedback`` with ``requires_feedback`` + ``feedback_field``
    + ``feedback_placeholder``;
  * non-halt choices carry a ``followup`` to ``orcho_run_resume`` with
    ready-to-send args.
  * ``retry_feedback`` can use native MCP elicitation when a capable
    client forwards the advertised args without prefilled feedback.

This is a single proof loop — every individual surface has its own
unit/integration test. What this file pins is that a fresh client can
discover Orcho, find the right workflow, observe a paused run, and
fetch the artefacts the run advertises, all through the public MCP
surface only.

Marked ``mcp_integration``; opt in with ``pytest -m mcp_integration``.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import AnyUrl

pytest.importorskip("mcp.client.stdio")

pytestmark = pytest.mark.mcp_integration


@pytest.fixture
def golden_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """Disposable copy of orcho-core's golden-api fixture.

    Same shape as ``test_demo_1b_single_project_mcp.py``'s fixture but
    returns ``(workspace_dir, project_dir)`` so the test can pass the
    project to ``orcho_run_start`` while still pinning ``ORCHO_WORKSPACE``
    for resource discovery.
    """
    core_root = Path(__file__).resolve().parents[4] / "orcho-core"
    src = core_root / "examples" / "golden-api"
    if not src.is_dir():
        pytest.skip(f"golden-api fixture not available at {src}")

    ws = tmp_path / "ws"
    project = ws / "demo_project"
    runs_dir = ws / "runspace" / "runs"
    runs_dir.mkdir(parents=True)
    shutil.copytree(src, project)
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    return ws, project


_KNOWN_HANDOFF_ACTIONS = frozenset(
    {"continue", "retry_feedback", "continue_with_waiver", "halt"}
)


@pytest.mark.anyio
async def test_self_discovery_flow_over_stdio(
    golden_project: tuple[Path, Path],
) -> None:
    """Cold MCP client walks the discover → observe → inspect loop.

    Algorithm:
      1. Spawn ``python -m orcho_mcp`` with ``ORCHO_WORKSPACE`` pinned.
      2. Call ``orcho_workspace_info`` to discover where Orcho reads/writes.
      3. Call ``orcho_workflows_list`` and read ``orcho://workflows``;
         assert both deliver the same recipe envelope.
      4. Spawn a mock run with ``mock_validate_plan_reject=3`` so the
         pipeline pauses on ``validate_plan``.
      5. Watch until ``handoff_or_terminal`` fires; validate the handoff
         hint's choices contract.
      6. Call ``orcho_run_status`` and resolve every advertised artefact
         URI through ``session.read_resource``.
      7. Call ``orcho_phase_handoff_decide`` with the advertised
         ``retry_feedback`` args and let native elicitation supply the
         missing feedback.
    """
    from mcp import ClientSession, StdioServerParameters, types as mcp_types
    from mcp.client.stdio import stdio_client

    ws, project = golden_project
    env = {**os.environ, "ORCHO_WORKSPACE": str(ws)}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "orcho_mcp"],
        env=env,
    )
    elicitation_requests: list[mcp_types.ElicitRequestParams] = []

    async def _elicitation_callback(_ctx, params):
        elicitation_requests.append(params)
        return mcp_types.ElicitResult(
            action="accept",
            content={
                "feedback": (
                    "Dogfood native elicitation: ask the reviewer to "
                    "reconsider the validation finding."
                ),
            },
        )

    async with stdio_client(params) as (read, write), ClientSession(
        read, write, elicitation_callback=_elicitation_callback,
    ) as session:
        await session.initialize()

        # 1) Discovery — workspace_info reveals where this instance lives.
        info_res = await session.call_tool("orcho_workspace_info", {})
        info = info_res.structuredContent
        assert info is not None
        assert info["workspace_dir"] == str(ws), (
            f"workspace_info should reflect $ORCHO_WORKSPACE; "
            f"got {info['workspace_dir']!r}"
        )
        assert info["runs_dir"], "workspace_info must expose runs_dir"

        # 2) Discovery — workflows_list tool surface.
        wf_tool_res = await session.call_tool("orcho_workflows_list", {})
        wf_tool = wf_tool_res.structuredContent
        assert wf_tool is not None
        assert wf_tool["format_version"] == 2
        recipe_names = {r["name"] for r in wf_tool["recipes"]}
        assert "plan_then_implement" in recipe_names
        assert "review_paused_run" in recipe_names

        # 3) Discovery — orcho://workflows resource. Same envelope.
        wf_resource = await session.read_resource(AnyUrl("orcho://workflows"))
        assert wf_resource.contents, "workflows resource returned no body"
        wf_body = json.loads(wf_resource.contents[0].text)
        assert wf_body["format_version"] == wf_tool["format_version"]
        assert {r["name"] for r in wf_body["recipes"]} == recipe_names

        # 4) Spawn a mock run that pauses on validate_plan. Advanced
        # declares human_feedback_on_reject on validate_plan; reject=3
        # forces every plan round to reject so the pause fires.
        start_res = await session.call_tool(
            "orcho_run_start",
            {
                "task": "self-discovery dogfood",
                "project_dir": str(project),
                "profile": "feature",
                "mock": True,
                "max_rounds": 1,
                "mock_validate_plan_reject": 3,
            },
        )
        start = start_res.structuredContent
        assert start is not None
        run_id = start["run_id"]
        assert run_id

        # 5) Observe — long-poll until the handoff (or terminal) fires.
        watch_res = await session.call_tool(
            "orcho_run_watch",
            {
                "run_id": run_id,
                "since_seq": 0,
                "until": "handoff_or_terminal",
                "timeout_s": 60,
                "interaction_client": "claude-code",
            },
            read_timeout_seconds=timedelta(seconds=65),
        )
        watch = watch_res.structuredContent
        assert watch is not None
        assert watch["triggered"] is True
        trigger_kind = watch["trigger"]["kind"]
        assert trigger_kind in {"handoff", "terminal"}, (
            f"expected handoff or terminal trigger, got {trigger_kind!r}"
        )

        # Handoff contract — choices never leak placeholder feedback,
        # only known verbs are surfaced, retry_feedback advertises its
        # collection contract, and non-halt choices carry a followup.
        if trigger_kind == "handoff":
            handoff = watch["handoff"]
            assert handoff is not None
            assert handoff["kind"] == "requires_user_decision"
            assert handoff["run_id"] == run_id
            choices = handoff["choices"]
            assert choices, "paused handoff must surface decision choices"
            retry_choice = None

            for choice in choices:
                action = choice["action"]
                assert action in _KNOWN_HANDOFF_ACTIONS, (
                    f"unknown action {action!r} leaked into choices"
                )
                assert "feedback" not in choice["args"], (
                    f"choices.args must never carry a placeholder "
                    f"feedback string; got {choice['args']!r}"
                )
                assert choice["tool"] == "orcho_phase_handoff_decide"
                assert choice["args"].get("run_id") == run_id

                if action == "retry_feedback":
                    retry_choice = choice
                    assert choice["requires_feedback"] is True
                    assert choice["feedback_field"] == "feedback"
                    assert choice["feedback_placeholder"], (
                        "retry_feedback must surface an operator-side "
                        "placeholder hint"
                    )
                    elicitation = choice["elicitation"]
                    assert elicitation is not None
                    assert elicitation["mode"] == "form"
                    assert elicitation["client_capability"] == (
                        "elicitation.form"
                    )
                    assert elicitation["field"] == "feedback"
                else:
                    assert choice["requires_feedback"] is False
                    assert choice["feedback_field"] is None
                    assert choice["feedback_placeholder"] is None
                    assert choice["elicitation"] is None

                followup = choice["followup"]
                if action == "halt":
                    assert followup is None, (
                        "halt is terminal — followup must be None"
                    )
                else:
                    assert followup is not None
                    assert followup["tool"] == "orcho_run_resume"
                    assert followup["args"] == {"run_id": run_id}

            assert retry_choice is not None

        # 6) Self-discovery — status surfaces readable artefacts.
        status_res = await session.call_tool(
            "orcho_run_status", {"run_id": run_id},
        )
        status = status_res.structuredContent
        assert status is not None
        artefacts = status.get("artefacts") or []
        assert isinstance(artefacts, list)

        # A paused validate_plan run has at minimum a parsed_plan
        # artefact — plan ran to completion before the gate fired.
        # Looser: also exercise evidence + diff when advertised, so the
        # dogfood proves "the URIs Orcho hands you actually resolve."
        if trigger_kind == "handoff":
            kinds = {a["kind"] for a in artefacts}
            assert "parsed_plan" in kinds, (
                f"validate_plan handoff should advertise parsed_plan; "
                f"got artefacts={artefacts!r}"
            )

        for art in artefacts:
            assert art["uri"].startswith(f"orcho://runs/{run_id}/"), (
                f"artefact URI must scope to this run; got {art['uri']!r}"
            )
            assert art["mime"], "artefact must declare a mime type"

            fetched = await session.read_resource(AnyUrl(art["uri"]))
            assert fetched.contents, (
                f"advertised artefact {art['uri']!r} returned no body"
            )
            first = fetched.contents[0]
            assert getattr(first, "text", None) is not None, (
                f"advertised artefact {art['uri']!r} returned non-text body"
            )

        # 7) Native elicitation — capable clients can forward the
        # retry_feedback choice args as advertised. The server requests
        # feedback via elicitation/create and records the accepted text.
        if trigger_kind == "handoff":
            decide_res = await session.call_tool(
                "orcho_phase_handoff_decide",
                retry_choice["args"],
            )
            decision = decide_res.structuredContent
            assert decision is not None
            assert decision["action"] == "retry_feedback"
            assert decision["feedback"] == (
                "Dogfood native elicitation: ask the reviewer to "
                "reconsider the validation finding."
            )
            assert elicitation_requests, (
                "retry_feedback without feedback should trigger native "
                "MCP elicitation when the client advertises support"
            )


@pytest.fixture
def anyio_backend():
    return "asyncio"
