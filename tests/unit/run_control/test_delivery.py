"""Unit tests for delivery-decision MCP behavior."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sdk import (
    NoWorkspace as _SDKNoWorkspace,
    RunNotFound as _SDKRunNotFound,
)

from orcho_mcp.errors import (
    InvalidPlanError,
    RunNotFoundError,
    WorkspaceNotResolvedError,
)
from orcho_mcp.run_control.delivery import decide_delivery


def _sdk_result(**overrides):
    data = {
        "run_id": "run1",
        "action": "approve",
        "accepted": True,
        "status": "committed",
        "terminal_outcome": "done",
        "halt_reason": None,
        "artifact_paths": ("/runs/run1/commit_decisions/run1.json",),
        "commit_sha": "abc123",
        "published_commit_sha": None,
        "blocker": None,
        "followup_run_id": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_decide_delivery_calls_sdk_with_cwd_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_decide(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return _sdk_result(action=args[1])

    monkeypatch.setattr(
        "orcho_mcp.run_control.delivery._sdk_decide_delivery",
        fake_decide,
    )

    result = decide_delivery("run1", "approve", note="ship it")

    assert result.run_id == "run1"
    assert result.accepted is True
    assert result.terminal_outcome == "done"
    assert result.artifact_paths == ["/runs/run1/commit_decisions/run1.json"]
    assert result.published_commit_sha is None
    assert calls == [
        {
            "args": ("run1", "approve"),
            "kwargs": {"note": "ship it", "cwd": None},
        },
    ]


@pytest.mark.parametrize(
    ("sdk_exc", "mapped"),
    [
        (_SDKRunNotFound("nope"), RunNotFoundError),
        (_SDKNoWorkspace("no workspace"), WorkspaceNotResolvedError),
        (ValueError("bad action"), InvalidPlanError),
    ],
)
def test_decide_delivery_maps_sdk_errors(
    sdk_exc: BaseException,
    mapped: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_decide(*args, **kwargs):
        raise sdk_exc

    monkeypatch.setattr(
        "orcho_mcp.run_control.delivery._sdk_decide_delivery",
        fake_decide,
    )

    with pytest.raises(mapped):
        decide_delivery("run1", "approve")


def test_decide_delivery_preserves_typed_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_decide(*args, **kwargs):
        return _sdk_result(
            accepted=False,
            status="not_applicable",
            terminal_outcome="halted",
            blocker="no_pending_delivery_gate",
            artifact_paths=(),
            commit_sha=None,
        )

    monkeypatch.setattr(
        "orcho_mcp.run_control.delivery._sdk_decide_delivery",
        fake_decide,
    )

    result = decide_delivery("run1", "approve")

    assert result.accepted is False
    assert result.status == "not_applicable"
    assert result.terminal_outcome == "halted"
    assert result.blocker == "no_pending_delivery_gate"


def test_decide_delivery_projects_published_commit_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "orcho_mcp.run_control.delivery._sdk_decide_delivery",
        lambda *args, **kwargs: _sdk_result(
            commit_sha=None,
            published_commit_sha="feed123",
        ),
    )

    result = decide_delivery("run1", "approve")

    assert result.commit_sha is None
    assert result.published_commit_sha == "feed123"
