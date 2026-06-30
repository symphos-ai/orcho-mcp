"""Unit tests for phase-handoff decision MCP behavior."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sdk import (
    InvalidPhaseHandoffState as _SDKInvalidPhaseHandoffState,
    NoWorkspace as _SDKNoWorkspace,
    RunNotFound as _SDKRunNotFound,
)

from orcho_mcp.errors import (
    InspectOnlyControlError,
    InvalidPlanError,
    RunNotFoundError,
    WorkspaceNotResolvedError,
)
from orcho_mcp.run_control.handoff import (
    decide_phase_handoff,
    decide_phase_handoff_with_elicitation,
)
from orcho_mcp.schemas import InspectOnlyControlResult, PhaseHandoffDecideResult
from tests.fixtures.mcp_workspace import meta, supervisor_state, write_run


class _Action:
    def to_dict(self) -> dict[str, object]:
        return {
            "intent": "Resume the run after recording the decision.",
            "tool": "orcho_run_resume",
            "args": {"run_id": "run1"},
        }


class _FakeSession:
    def __init__(self, *, supports_elicitation: bool) -> None:
        self.supports_elicitation = supports_elicitation

    def check_client_capability(self, _capability) -> bool:
        return self.supports_elicitation


class _FakeContext:
    def __init__(
        self,
        *,
        supports_elicitation: bool,
        feedback: str,
        elicitation_action: str = "accept",
    ) -> None:
        self.session = _FakeSession(supports_elicitation=supports_elicitation)
        self.feedback = feedback
        self.elicitation_action = elicitation_action
        self.elicit_calls: list[dict[str, object]] = []

    async def elicit(self, *, message: str, schema):
        self.elicit_calls.append({"message": message, "schema": schema})
        return SimpleNamespace(
            action=self.elicitation_action,
            data=SimpleNamespace(feedback=self.feedback),
        )


# Both verbs gate on a non-empty ``feedback`` and share the elicitation
# path, so the behavioural contract is parametrized over both.
_FEEDBACK_ACTIONS = ["retry_feedback", "continue_with_waiver"]


def _sdk_result(*, action: str, feedback: str | None):
    return SimpleNamespace(
        run_id="run1",
        handoff_id="handoff1",
        phase="validate_plan",
        action=action,
        feedback=feedback,
        note=None,
        decided_at="2026-05-25T00:00:00Z",
        next_actions=[_Action()],
    )


# ── Stage-5 no-change guard: decide stays a direct SDK call with cwd=None ───
#
# decide_phase_handoff is intentionally NOT routed through
# sdk.run_control.RunService.decide_handoff: RunService forwards only
# command.to_decide_kwargs() (run_id/handoff_id/action/feedback/note) and
# never passes cwd, so the SDK default (_CWD_DEFAULT) would re-enable
# walk-up. The MCP boundary must resolve strictly from the ambient
# workspace, so it calls sdk.phase_handoff_decide directly with cwd=None.
# This guard pins that contract.


def test_decide_phase_handoff_calls_sdk_with_cwd_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_decide(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return _sdk_result(action=args[2], feedback=kwargs.get("feedback"))

    monkeypatch.setattr(
        "orcho_mcp.run_control.handoff._sdk_phase_handoff_decide",
        fake_decide,
    )

    decide_phase_handoff("run1", "handoff1", "continue")

    assert len(calls) == 1
    # Positional run_id/handoff_id/action, and the load-bearing cwd=None.
    assert calls[0]["args"] == ("run1", "handoff1", "continue")
    assert "cwd" in calls[0]["kwargs"]
    assert calls[0]["kwargs"]["cwd"] is None


# ── SDK→MCP error mapping is owned by services.errors.map_sdk_errors ───
#
# decide_phase_handoff no longer catches SDK exception types itself; it
# wraps the SDK call in the shared owner. These cases pin the canonical
# taxonomy stays consistent with the read paths (run_reads / inspection).


@pytest.mark.parametrize(
    ("sdk_exc", "mapped"),
    [
        (_SDKRunNotFound("nope"), RunNotFoundError),
        (_SDKNoWorkspace("no workspace"), WorkspaceNotResolvedError),
        (ValueError("bad action"), InvalidPlanError),
        (_SDKInvalidPhaseHandoffState("wrong status"), InvalidPlanError),
    ],
)
def test_decide_phase_handoff_maps_sdk_errors(
    sdk_exc: BaseException,
    mapped: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_decide(*args, **kwargs):
        raise sdk_exc

    monkeypatch.setattr(
        "orcho_mcp.run_control.handoff._sdk_phase_handoff_decide",
        fake_decide,
    )

    with pytest.raises(mapped):
        decide_phase_handoff("run1", "handoff1", "continue")


def test_decide_phase_handoff_run_not_found_message_includes_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_decide(*args, **kwargs):
        raise _SDKRunNotFound("nope")

    monkeypatch.setattr(
        "orcho_mcp.run_control.handoff._sdk_phase_handoff_decide",
        fake_decide,
    )

    with pytest.raises(RunNotFoundError, match="run not found: run1"):
        decide_phase_handoff("run1", "handoff1", "continue")


@pytest.mark.parametrize("action", _FEEDBACK_ACTIONS)
@pytest.mark.asyncio
async def test_feedback_action_uses_native_elicitation_when_supported(
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capable client can send choice args without prefilled feedback."""
    calls: list[dict[str, object]] = []

    def fake_decide(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return _sdk_result(action=args[2], feedback=kwargs["feedback"])

    monkeypatch.setattr(
        "orcho_mcp.run_control.handoff._sdk_phase_handoff_decide",
        fake_decide,
    )
    ctx = _FakeContext(
        supports_elicitation=True,
        feedback="Please revisit the database migration step.",
    )

    result = await decide_phase_handoff_with_elicitation(
        "run1",
        "handoff1",
        action,
        ctx=ctx,  # type: ignore[arg-type]
    )

    assert result.action == action
    assert result.feedback == "Please revisit the database migration step."
    # Single non-optional resume followup rides in next_actions.
    assert result.next_actions[0].tool == "orcho_run_resume"
    assert len(ctx.elicit_calls) == 1
    assert ctx.elicit_calls[0]["schema"].model_fields["feedback"]
    assert calls[0]["kwargs"]["feedback"] == result.feedback


@pytest.mark.parametrize("action", _FEEDBACK_ACTIONS)
@pytest.mark.asyncio
async def test_feedback_action_without_capability_keeps_existing_fallback(
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without elicitation support, the agent still supplies feedback."""
    calls: list[dict[str, object]] = []

    def fake_decide(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return _sdk_result(action=args[2], feedback=kwargs["feedback"])

    monkeypatch.setattr(
        "orcho_mcp.run_control.handoff._sdk_phase_handoff_decide",
        fake_decide,
    )
    ctx = _FakeContext(supports_elicitation=False, feedback="unused")

    result = await decide_phase_handoff_with_elicitation(
        "run1",
        "handoff1",
        action,
        feedback="User supplied chat feedback.",
        ctx=ctx,  # type: ignore[arg-type]
    )

    assert result.action == action
    assert result.feedback == "User supplied chat feedback."
    assert ctx.elicit_calls == []
    assert calls[0]["kwargs"]["feedback"] == "User supplied chat feedback."


@pytest.mark.parametrize("action", _FEEDBACK_ACTIONS)
@pytest.mark.asyncio
async def test_feedback_action_elicitation_decline_is_actionable(
    action: str,
) -> None:
    """Declined elicitation tells the agent how to fall back to chat."""
    ctx = _FakeContext(
        supports_elicitation=True,
        feedback="unused",
        elicitation_action="decline",
    )

    with pytest.raises(InvalidPlanError) as exc_info:
        await decide_phase_handoff_with_elicitation(
            "run1",
            "handoff1",
            action,
            ctx=ctx,  # type: ignore[arg-type]
        )

    message = str(exc_info.value)
    assert message.startswith(f"{action} requires feedback")
    assert "Native MCP elicitation was decline by the client" in message
    assert "ask the user for feedback in chat" in message
    assert "retry with args.feedback" in message


# ── Control guard (T3): inspect_only short-circuit on both decide entries ────
#
# A run NOT started by this MCP server (no durable ``mcp_supervisor.json``) is
# refused by *raising* InspectOnlyControlError BEFORE the SDK call and BEFORE any
# decision artifact is written — on both the sync ``decide_phase_handoff`` and
# the async ``decide_phase_handoff_with_elicitation`` entry points. Raising (not
# returning a success-union member) keeps orcho_phase_handoff_decide's success
# outputSchema unchanged. The carried ``result`` is the typed
# InspectOnlyControlResult: the CLI-control instruction rides only in message /
# suggested_next_action; next_actions stay read-only MCP inspection.

_HANDOFF = {
    "id": "validate_plan:plan_round:1",
    "phase": "validate_plan",
    "available_actions": ["continue", "retry_feedback", "halt"],
}


def _paused_foreign_run(fake_workspace, run_id: str) -> None:
    # Foreign / CLI-started: paused on a handoff but NO mcp_supervisor.json.
    write_run(
        fake_workspace, run_id,
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x", task="t",
            phase_handoff=_HANDOFF,
        ),
    )


def _paused_controllable_run(fake_workspace, run_id: str):
    # MCP-started: same paused state plus durable supervisor metadata.
    return write_run(
        fake_workspace, run_id,
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x", task="t",
            phase_handoff=_HANDOFF,
        ),
        supervisor_state=supervisor_state(run_id=run_id, project_dir="/p/x"),
    )


def _assert_decide_inspect_only(result, run_id: str) -> None:
    assert isinstance(result, InspectOnlyControlResult)
    assert result.kind == "inspect_only"
    assert result.control == "inspect_only"
    assert result.attempted == "phase_handoff_decide"
    assert result.run_id == run_id
    # CLI-control instruction lives in free text only.
    assert "CLI" in result.message
    assert "CLI" in result.suggested_next_action
    # next_actions: read-only MCP inspection ONLY.
    assert result.next_actions
    assert all(na.kind == "ready_call" for na in result.next_actions)
    assert {na.tool for na in result.next_actions} == {
        "orcho_run_status", "orcho_run_evidence",
    }
    assert all(na.tool != "orcho_run_resume" for na in result.next_actions)
    # Each record is a valid ready_call to the inspection tool it names: status
    # carries just the run_id; evidence MUST pin slice='errors' so a builder
    # regression that drops or changes the slice fails here, not silently.
    for na in result.next_actions:
        assert na.args.get("run_id") == run_id
        if na.tool == "orcho_run_evidence":
            assert na.args.get("slice") == "errors", na.args


def _spy_sdk(monkeypatch) -> list:
    calls: list = []

    def fake_decide(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return _sdk_result(action=args[2], feedback=kwargs.get("feedback"))

    monkeypatch.setattr(
        "orcho_mcp.run_control.handoff._sdk_phase_handoff_decide",
        fake_decide,
    )
    return calls


def test_decide_sync_foreign_run_is_inspect_only(fake_workspace, monkeypatch):
    _paused_foreign_run(fake_workspace, "20260101_000020")
    run_dir = fake_workspace / "runspace" / "runs" / "20260101_000020"
    calls = _spy_sdk(monkeypatch)

    with pytest.raises(InspectOnlyControlError) as exc:
        decide_phase_handoff("20260101_000020", _HANDOFF["id"], "continue")

    _assert_decide_inspect_only(exc.value.result, "20260101_000020")
    # The SDK decide was never called …
    assert calls == []
    # … and no decision artifact was written.
    assert not (run_dir / "phase_handoff_decisions").exists()


@pytest.mark.asyncio
async def test_decide_elicitation_foreign_run_is_inspect_only(
    fake_workspace, monkeypatch,
):
    _paused_foreign_run(fake_workspace, "20260101_000021")
    run_dir = fake_workspace / "runspace" / "runs" / "20260101_000021"
    calls = _spy_sdk(monkeypatch)
    # A capable client + feedback-gated action: the control guard must fire
    # BEFORE native elicitation, so elicit is never called either.
    ctx = _FakeContext(supports_elicitation=True, feedback="unused")

    with pytest.raises(InspectOnlyControlError) as exc:
        await decide_phase_handoff_with_elicitation(
            "20260101_000021",
            _HANDOFF["id"],
            "retry_feedback",
            ctx=ctx,  # type: ignore[arg-type]
        )

    _assert_decide_inspect_only(exc.value.result, "20260101_000021")
    assert calls == []
    assert ctx.elicit_calls == []
    assert not (run_dir / "phase_handoff_decisions").exists()


def test_decide_sync_controllable_run_keeps_existing_behavior(
    fake_workspace, monkeypatch,
):
    _paused_controllable_run(fake_workspace, "20260101_000022")
    calls = _spy_sdk(monkeypatch)

    result = decide_phase_handoff(
        "20260101_000022", _HANDOFF["id"], "continue",
    )

    # mcp_controllable run: prior behavior — real PhaseHandoffDecideResult and
    # the SDK decide was invoked.
    assert isinstance(result, PhaseHandoffDecideResult)
    assert result.action == "continue"
    assert len(calls) == 1
    assert calls[0]["args"][0] == "20260101_000022"


@pytest.mark.asyncio
async def test_decide_elicitation_controllable_run_keeps_existing_behavior(
    fake_workspace, monkeypatch,
):
    _paused_controllable_run(fake_workspace, "20260101_000023")
    calls = _spy_sdk(monkeypatch)
    ctx = _FakeContext(
        supports_elicitation=True,
        feedback="Please revisit the migration step.",
    )

    result = await decide_phase_handoff_with_elicitation(
        "20260101_000023",
        _HANDOFF["id"],
        "retry_feedback",
        ctx=ctx,  # type: ignore[arg-type]
    )

    assert isinstance(result, PhaseHandoffDecideResult)
    assert result.action == "retry_feedback"
    # Controllable run: native elicitation ran and the SDK decide was invoked.
    assert len(ctx.elicit_calls) == 1
    assert len(calls) == 1
