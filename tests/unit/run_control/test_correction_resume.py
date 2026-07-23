from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orcho_mcp.run_control.lifecycle import resume_run


def _decision(*, blocked: bool = False):
    return SimpleNamespace(
        continuation_subject="retained_change",
        blocked=blocked,
        diff_source="none" if blocked else "worktree",
        reason="retained worktree unavailable" if blocked else "retained change",
    )


def _followup_preflight():
    return SimpleNamespace(
        resolution=SimpleNamespace(operation="start_followup", blocker=None),
    )


@pytest.mark.asyncio
async def test_bare_correction_resume_requires_operator_input(monkeypatch) -> None:
    monkeypatch.setattr(
        "orcho_mcp.services.continuation.resolve_core_continuation",
        lambda _run_id: _decision(),
    )

    result = await resume_run("parent")

    assert result.resume_outcome == "operator_input_required"
    assert [action.model_dump() for action in result.next_actions] == [{
        "intent": "Choose whether to launch the retained-change correction follow-up.",
        "tool": "orcho_run_resume",
        "args": {"run_id": "parent"},
        "optional": False,
        "kind": "operator_input_required",
        "requires_operator_input": True,
        "choices": ["followup", "exit"],
        "input_schema": result.next_actions[0].input_schema,
        "context": None,
    }]


@pytest.mark.asyncio
async def test_correction_exit_does_not_get_supervisor(monkeypatch) -> None:
    monkeypatch.setattr(
        "orcho_mcp.services.continuation.resolve_core_continuation",
        lambda _run_id: _decision(),
    )
    monkeypatch.setattr(
        "orcho_mcp.supervisor.get_supervisor",
        lambda: pytest.fail("exit must not spawn"),
    )

    result = await resume_run("parent", operator_intent="exit")

    assert result.resume_outcome == "exit"


@pytest.mark.asyncio
async def test_correction_followup_launches_child(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "orcho_mcp.services.continuation.resolve_core_continuation",
        lambda _run_id: _decision(),
    )
    preflight_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "orcho_mcp.services.continuation.preflight_core_continuation",
        lambda run_id, **kwargs: (
            preflight_calls.append({"run_id": run_id, **kwargs})
            or _followup_preflight()
        ),
    )
    calls: list[tuple[str, str]] = []
    handle = SimpleNamespace(
        run_id="child", run_dir=tmp_path / "child", pid=42, started_at="now",
        project_dir="/project", command=["orcho-run"],
    )

    class _Supervisor:
        async def followup(self, *, parent_run_id: str, operator_comment: str):
            calls.append((parent_run_id, operator_comment))
            return handle

    monkeypatch.setattr("orcho_mcp.supervisor.get_supervisor", lambda: _Supervisor())
    result = await resume_run(
        "parent", operator_intent="followup", operator_comment="fix test",
    )

    assert result.resume_outcome == "followup_started"
    assert result.parent_run_id == "parent"
    assert result.run_id == "child"
    assert calls == [("parent", "fix test")]
    assert preflight_calls == [{
        "run_id": "parent", "intent": "followup", "operator_comment": "fix test",
    }]


@pytest.mark.asyncio
async def test_checkpoint_followup_uses_core_blocker_without_same_run_resume(monkeypatch) -> None:
    """Explicit followup never silently degrades to checkpoint resume."""
    monkeypatch.setattr(
        "orcho_mcp.services.continuation.resolve_core_continuation",
        lambda _run_id: SimpleNamespace(
            continuation_subject="checkpoint", blocked=False, diff_source=None,
            reason="checkpoint resumable",
        ),
    )
    preflight_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "orcho_mcp.services.continuation.preflight_core_continuation",
        lambda run_id, **kwargs: (
            preflight_calls.append({"run_id": run_id, **kwargs})
            or SimpleNamespace(resolution=SimpleNamespace(
                operation="blocked", blocker="follow-up requires a retained-change continuation subject",
            ))
        ),
    )
    monkeypatch.setattr(
        "orcho_mcp.supervisor.get_supervisor",
        lambda: pytest.fail("blocked followup must not call supervisor.resume"),
    )

    result = await resume_run(
        "checkpoint-parent", operator_intent="followup", operator_comment="continue separately",
    )

    assert result.resume_outcome == "blocked"
    assert preflight_calls == [{
        "run_id": "checkpoint-parent",
        "intent": "followup",
        "operator_comment": "continue separately",
    }]


@pytest.mark.asyncio
async def test_blank_correction_comment_does_not_get_supervisor(monkeypatch) -> None:
    monkeypatch.setattr(
        "orcho_mcp.services.continuation.resolve_core_continuation",
        lambda _run_id: _decision(),
    )
    monkeypatch.setattr(
        "orcho_mcp.supervisor.get_supervisor",
        lambda: pytest.fail("blank comment must not spawn"),
    )

    result = await resume_run(
        "parent", operator_intent="followup", operator_comment=" ",
    )

    assert result.resume_outcome == "operator_input_required"


@pytest.mark.asyncio
async def test_blocked_correction_does_not_elicit_or_spawn(monkeypatch) -> None:
    monkeypatch.setattr(
        "orcho_mcp.services.continuation.resolve_core_continuation",
        lambda _run_id: _decision(blocked=True),
    )
    monkeypatch.setattr(
        "orcho_mcp.run_control.lifecycle._elicit_correction_input",
        lambda _ctx: pytest.fail("blocked correction must not elicit"),
    )

    result = await resume_run("parent")

    assert result.resume_outcome == "blocked"
    assert result.blocked is True


@pytest.mark.asyncio
async def test_native_elicitation_accepts_then_launches_followup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "orcho_mcp.services.continuation.resolve_core_continuation",
        lambda _run_id: _decision(),
    )
    monkeypatch.setattr(
        "orcho_mcp.services.continuation.preflight_core_continuation",
        lambda *_args, **_kwargs: _followup_preflight(),
    )
    monkeypatch.setattr(
        "orcho_mcp.run_control.handoff._client_supports_form_elicitation",
        lambda _ctx: True,
    )

    class _Context:
        async def elicit(self, **_kwargs):
            return SimpleNamespace(
                action="accept",
                data=SimpleNamespace(operator_intent="followup", operator_comment="fix it"),
            )

    calls: list[tuple[str, str]] = []
    handle = SimpleNamespace(
        run_id="child", run_dir=tmp_path / "child", pid=1, started_at="now",
        project_dir="/project", command=["orcho-run"],
    )

    class _Supervisor:
        async def followup(self, *, parent_run_id: str, operator_comment: str):
            calls.append((parent_run_id, operator_comment))
            return handle

    monkeypatch.setattr("orcho_mcp.supervisor.get_supervisor", lambda: _Supervisor())
    result = await resume_run("parent", ctx=_Context())

    assert result.resume_outcome == "followup_started"
    assert calls == [("parent", "fix it")]


@pytest.mark.asyncio
async def test_native_elicitation_decline_returns_typed_input_without_spawn(monkeypatch) -> None:
    monkeypatch.setattr(
        "orcho_mcp.services.continuation.resolve_core_continuation",
        lambda _run_id: _decision(),
    )
    monkeypatch.setattr(
        "orcho_mcp.run_control.handoff._client_supports_form_elicitation",
        lambda _ctx: True,
    )

    class _Context:
        async def elicit(self, **_kwargs):
            return SimpleNamespace(action="decline", data=None)

    monkeypatch.setattr(
        "orcho_mcp.supervisor.get_supervisor",
        lambda: pytest.fail("declined elicitation must not spawn"),
    )
    result = await resume_run("parent", ctx=_Context())

    assert result.resume_outcome == "operator_input_required"
