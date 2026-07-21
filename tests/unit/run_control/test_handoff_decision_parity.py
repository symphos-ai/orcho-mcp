"""Phase-handoff decision API parity (T1D / review F1).

Pins that ``orcho_phase_handoff_decide`` (via ``decide_phase_handoff`` and
the elicitation wrapper) maps all four actions to the SDK call and a
``PhaseHandoffDecideResult``, that a missing required ``feedback`` raises a
structured ``InvalidPlanError`` (naming the field, before the SDK call —
not an opaque traceback), and that ``note`` / ``feedback`` thread through.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.run_control.handoff import (
    decide_phase_handoff,
    decide_phase_handoff_with_elicitation,
)
from orcho_mcp.schemas import PhaseHandoffDecideResult

_FEEDBACK_ACTIONS = ["retry_feedback", "continue_with_waiver"]
_NON_FEEDBACK_ACTIONS = ["continue", "halt"]
_ALL_ACTIONS = _NON_FEEDBACK_ACTIONS + _FEEDBACK_ACTIONS


class _Action:
    def to_dict(self) -> dict[str, object]:
        return {
            "intent": "Resume the run after recording the decision.",
            "tool": "orcho_run_resume",
            "args": {"run_id": "run1"},
            "optional": False,
        }


def _install_fake_sdk(monkeypatch) -> list[dict[str, object]]:
    """Patch the SDK decide; echo action/feedback/note into the result."""
    calls: list[dict[str, object]] = []

    def fake_decide(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(
            run_id=args[0],
            handoff_id=args[1],
            phase="validate_plan",
            action=args[2],
            feedback=kwargs.get("feedback"),
            note=kwargs.get("note"),
            decided_at="2026-05-25T00:00:00Z",
            next_actions=[_Action()],
        )

    monkeypatch.setattr(
        "orcho_mcp.run_control.handoff._sdk_phase_handoff_decide", fake_decide,
    )
    monkeypatch.setattr(
        "orcho_mcp.run_control.handoff.find_run_dir", lambda run_id: Path("/runs") / run_id,
    )
    return calls


# ── (a) all four actions map to the SDK call + PhaseHandoffDecideResult ──────


@pytest.mark.parametrize("action", _ALL_ACTIONS)
def test_each_action_maps_to_sdk_and_result(action, monkeypatch):
    calls = _install_fake_sdk(monkeypatch)
    feedback = "Revisit the migration." if action in _FEEDBACK_ACTIONS else None

    result = decide_phase_handoff(
        "run1", "handoff1", action, feedback=feedback, note="audit",
    )

    # Mapped to one SDK call with positional run_id/handoff_id/action +
    # the load-bearing cwd=None, and feedback/note threaded through.
    assert len(calls) == 1
    assert calls[0]["args"] == ("run1", "handoff1", action)
    assert calls[0]["kwargs"]["cwd"] is None
    assert calls[0]["kwargs"]["runs_dir"] == Path("/runs")
    assert calls[0]["kwargs"]["feedback"] == feedback
    assert calls[0]["kwargs"]["note"] == "audit"

    # Mapped back to the typed wire result.
    assert isinstance(result, PhaseHandoffDecideResult)
    assert result.run_id == "run1"
    assert result.handoff_id == "handoff1"
    assert result.phase == "validate_plan"
    assert result.action == action
    assert result.feedback == feedback
    assert result.note == "audit"
    assert result.decided_at == "2026-05-25T00:00:00Z"
    assert result.next_actions[0].tool == "orcho_run_resume"
    assert result.next_actions[0].optional is False


# ── (b) missing feedback → structured validation error, before the SDK ───────


@pytest.mark.parametrize("action", _FEEDBACK_ACTIONS)
def test_missing_feedback_raises_structured_error_before_sdk(
    action, monkeypatch,
):
    calls = _install_fake_sdk(monkeypatch)

    with pytest.raises(InvalidPlanError) as exc:
        decide_phase_handoff("run1", "handoff1", action)

    # Names the exact field that is required, and the SDK was never called.
    assert "feedback" in str(exc.value)
    assert action in str(exc.value)
    assert calls == []


@pytest.mark.parametrize("action", _FEEDBACK_ACTIONS)
def test_whitespace_feedback_raises_structured_error(action, monkeypatch):
    calls = _install_fake_sdk(monkeypatch)

    with pytest.raises(InvalidPlanError):
        decide_phase_handoff("run1", "handoff1", action, feedback="   ")

    assert calls == []


@pytest.mark.parametrize("action", _FEEDBACK_ACTIONS)
@pytest.mark.asyncio
async def test_missing_feedback_without_elicitation_is_structured(
    action, monkeypatch,
):
    """No elicitation support + no feedback → structured error, not a leak."""
    calls = _install_fake_sdk(monkeypatch)
    ctx = SimpleNamespace(
        session=SimpleNamespace(check_client_capability=lambda _c: False),
    )

    with pytest.raises(InvalidPlanError) as exc:
        await decide_phase_handoff_with_elicitation(
            "run1", "handoff1", action, ctx=ctx,  # type: ignore[arg-type]
        )

    assert "feedback" in str(exc.value)
    assert calls == []


@pytest.mark.parametrize("action", _NON_FEEDBACK_ACTIONS)
def test_non_feedback_actions_do_not_require_feedback(action, monkeypatch):
    calls = _install_fake_sdk(monkeypatch)

    result = decide_phase_handoff("run1", "handoff1", action)

    assert result.action == action
    assert len(calls) == 1


# ── (c) note / feedback threading ────────────────────────────────────────────


def test_note_and_feedback_thread_through(monkeypatch):
    calls = _install_fake_sdk(monkeypatch)

    result = decide_phase_handoff(
        "run1", "handoff1", "retry_feedback",
        feedback="Tighten the acceptance criteria.", note="operator note",
    )

    assert calls[0]["kwargs"]["feedback"] == "Tighten the acceptance criteria."
    assert calls[0]["kwargs"]["note"] == "operator note"
    assert result.feedback == "Tighten the acceptance criteria."
    assert result.note == "operator note"


# ── (d) decide-command carries no resolution context (keep-surface guard) ─────


def test_to_decide_kwargs_carries_no_resolution_context():
    """Executable justification for keeping MCP ``decide`` on the direct
    ``sdk.phase_handoff_decide(cwd=None)`` call instead of routing through
    ``RunService.decide_handoff``.

    ``RunService.decide_handoff`` delegates via
    ``command.to_decide_kwargs()``; that command DTO carries **no**
    ``cwd`` / ``workspace`` / ``runs_dir`` field, so it cannot express the
    load-bearing ``cwd=None`` (no ambient walk-up) the MCP boundary pins.
    This guard fixes the exact kwargs the command produces:

    - it is exactly ``{run_id, handoff_id, action, feedback, note}``;
    - it contains none of ``cwd`` / ``workspace`` / ``runs_dir``.

    If core later widens ``PhaseHandoffDecisionCommand`` with a resolution
    field, this test fails and signals that the keep decision in
    ``docs/architecture/mcp_boundaries.md`` (surface c) must be re-evaluated.
    """
    from sdk.run_control import PhaseHandoffDecisionCommand

    command = PhaseHandoffDecisionCommand(
        run_id="r", handoff_id="h", action="continue",
    )
    kwargs = command.to_decide_kwargs()

    assert set(kwargs) == {"run_id", "handoff_id", "action", "feedback", "note"}
    for forbidden in ("cwd", "workspace", "runs_dir"):
        assert forbidden not in kwargs, (
            f"PhaseHandoffDecisionCommand.to_decide_kwargs() unexpectedly "
            f"carries {forbidden!r}: core now expresses run-resolution "
            "context on the decide command, so MCP could route decide "
            "through RunService.decide_handoff. Re-evaluate surface (c) in "
            "docs/architecture/mcp_boundaries.md."
        )
