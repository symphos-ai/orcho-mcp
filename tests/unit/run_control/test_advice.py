"""Unit tests for the ``orcho_handoff_advice`` advisory tool.

The SDK accessor (orcho-core's ``request_handoff_advice``, tested in orcho-core)
is monkeypatched at this module's own seam, so these tests cover only the MCP
layer: the typed wire projection, the deterministic ``ready_next_action`` (a
pre-filled ``orcho_phase_handoff_decide`` call carrying mandatory provenance for
the existing ``retry_feedback`` verb), the no-auto-apply contract for non-retry
recommendations, and the SDK→MCP error mapping. L2: the tool is registered.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sdk import (
    InvalidPhaseHandoffState as _SDKInvalidPhaseHandoffState,
    NoWorkspace as _SDKNoWorkspace,
    RunNotFound as _SDKRunNotFound,
)

from orcho_mcp.errors import (
    InvalidPlanError,
    RunNotFoundError,
    WorkspaceNotResolvedError,
)
from orcho_mcp.run_control.advice import request_advice
from orcho_mcp.schemas import HandoffAdviceResult

_SEAM = "orcho_mcp.run_control.advice._sdk_request_handoff_advice"


@pytest.fixture(autouse=True)
def _resolved_run_dir(monkeypatch):
    monkeypatch.setattr(
        "orcho_mcp.run_control.advice.find_run_dir",
        lambda run_id: Path("/runs") / run_id,
    )


def _safety(**kw):
    base = dict(
        auto_apply_ok=True, needs_confirmation=False,
        blocked_reason="", waiver_blocked=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _sdk_advice(**kw):
    """Build a stand-in for orcho-core's HandoffAdviceResult dataclass."""
    base = dict(
        run_id="20260623_120000_aaaaaa",
        handoff_id="review_changes:review:2",
        phase="review_changes",
        recommended_action="retry_feedback",
        confidence="high",
        rationale="bounded fix",
        retry_feedback="close the named gap and re-run verification",
        risks=("scope creep",),
        expected_files=("a.py",),
        operator_note="ok",
        parse_warnings=(),
        safety=_safety(),
        advice_artifact="phase_handoff_advice/review_changes_review_2_abc.json",
        provenance_note=(
            "feedback_source=agent_advice; "
            "advice_artifact=phase_handoff_advice/review_changes_review_2_abc.json"
        ),
        usage={"tokens_in": 100, "tokens_out": 50},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _patch(monkeypatch, advice):
    monkeypatch.setattr(_SEAM, lambda *a, **k: advice)


# ── eligible retry_feedback → typed advice + provenance-carrying ready_call ──


def test_eligible_retry_returns_typed_advice_with_provenance(monkeypatch) -> None:
    _patch(monkeypatch, _sdk_advice())

    result = request_advice("20260623_120000_aaaaaa")

    assert isinstance(result, HandoffAdviceResult)
    assert result.run_id == "20260623_120000_aaaaaa"
    assert result.handoff_id == "review_changes:review:2"
    assert result.phase == "review_changes"
    assert result.recommended_action == "retry_feedback"
    assert result.confidence == "high"
    assert result.rationale == "bounded fix"
    assert result.retry_feedback == "close the named gap and re-run verification"
    assert result.risks == ["scope creep"]
    assert result.expected_files == ["a.py"]
    assert result.operator_note == "ok"
    assert result.safety.auto_apply_ok is True
    assert result.safety.needs_confirmation is False
    assert result.advice_artifact.startswith("phase_handoff_advice/")
    assert result.provenance_note
    assert result.usage == {"tokens_in": 100, "tokens_out": 50}

    # ready_next_action is a ready_call to the EXISTING decide verb, pre-filled
    # with feedback AND the mandatory provenance note.
    ready = result.ready_next_action
    assert ready is not None
    assert ready.tool == "orcho_phase_handoff_decide"
    assert ready.kind == "ready_call"
    assert ready.args["run_id"] == "20260623_120000_aaaaaa"
    assert ready.args["handoff_id"] == "review_changes:review:2"
    assert ready.args["action"] == "retry_feedback"
    assert ready.args["feedback"] == result.retry_feedback
    assert ready.args["note"] == result.provenance_note
    assert ready.args["note"]  # mandatory, non-empty provenance


def test_passes_handoff_id_to_sdk(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def _capture(run_id, handoff_id=None, **kw):
        seen["run_id"] = run_id
        seen["handoff_id"] = handoff_id
        seen["cwd"] = kw.get("cwd", "MISSING")
        return _sdk_advice()

    monkeypatch.setattr(_SEAM, _capture)

    request_advice("20260623_120000_aaaaaa", "review_changes:review:2")
    assert seen["run_id"] == "20260623_120000_aaaaaa"
    assert seen["handoff_id"] == "review_changes:review:2"
    # The MCP boundary always disables cwd walk-up.
    assert seen["cwd"] is None


def test_low_confidence_still_forms_ready_call(monkeypatch) -> None:
    _patch(monkeypatch, _sdk_advice(
        confidence="low",
        safety=_safety(auto_apply_ok=False, needs_confirmation=True),
    ))

    result = request_advice("20260623_120000_aaaaaa")

    assert result.confidence == "low"
    assert result.safety.needs_confirmation is True
    # ready_next_action is still formed for a low-confidence retry.
    assert result.ready_next_action is not None
    assert result.ready_next_action.kind == "ready_call"
    assert result.ready_next_action.args["action"] == "retry_feedback"
    assert result.ready_next_action.args["note"] == result.provenance_note


# ── non-retry recommendations: reflect without auto-applying ─────────────────


def test_halt_recommendation_reflected_without_auto_apply(monkeypatch) -> None:
    _patch(monkeypatch, _sdk_advice(
        recommended_action="halt",
        retry_feedback="",
        safety=_safety(auto_apply_ok=False, blocked_reason="halt is not a retry"),
    ))

    result = request_advice("20260623_120000_aaaaaa")

    assert result.recommended_action == "halt"
    assert result.safety.auto_apply_ok is False
    ready = result.ready_next_action
    assert ready is not None
    # Reflects the recommendation (mirrors the verb) — but the tool applied
    # nothing; the operator chooses whether to forward this call.
    assert ready.tool == "orcho_phase_handoff_decide"
    assert ready.args["action"] == "halt"
    assert "feedback" not in ready.args
    assert ready.args["note"] == result.provenance_note


def test_continue_with_waiver_requires_operator_input(monkeypatch) -> None:
    _patch(monkeypatch, _sdk_advice(
        recommended_action="continue_with_waiver",
        retry_feedback="",
        safety=_safety(auto_apply_ok=False, waiver_blocked=True,
                       blocked_reason="waiver is advisory only"),
    ))

    result = request_advice("20260623_120000_aaaaaa")

    assert result.recommended_action == "continue_with_waiver"
    ready = result.ready_next_action
    assert ready is not None
    assert ready.tool == "orcho_phase_handoff_decide"
    # The operator must supply the waiver verdict; args intentionally omit it.
    assert ready.kind == "operator_input_required"
    assert ready.requires_operator_input is True
    assert "feedback" not in ready.args
    assert ready.input_schema is not None and "feedback" in ready.input_schema


def test_unparseable_advice_has_empty_provenance(monkeypatch) -> None:
    # Unparseable advisor output: normalised to halt/low, no durable write.
    _patch(monkeypatch, _sdk_advice(
        recommended_action="halt",
        confidence="low",
        retry_feedback="",
        parse_warnings=("advice_unparseable",),
        advice_artifact="",
        provenance_note="",
        safety=_safety(auto_apply_ok=False, needs_confirmation=True),
    ))

    result = request_advice("20260623_120000_aaaaaa")

    assert "advice_unparseable" in result.parse_warnings
    assert result.advice_artifact == ""
    assert result.provenance_note == ""
    # A ready_next_action is still formed (halt), but it carries no provenance.
    assert result.ready_next_action is not None
    assert result.ready_next_action.args["action"] == "halt"
    assert result.ready_next_action.args["note"] == ""


# ── mock-run advisor provider seam (hermetic in-process advice) ──────────────


def _capture_provider(monkeypatch) -> dict:
    """Patch the SDK seam to record the ``provider`` it was handed."""
    seen: dict[str, object] = {}

    def _capture(run_id, handoff_id=None, **kw):
        seen["provider"] = kw.get("provider", "MISSING")
        return _sdk_advice()

    monkeypatch.setattr(_SEAM, _capture)
    return seen


def test_mock_run_resolves_mock_provider(monkeypatch, tmp_path) -> None:
    # A run launched with mock=True records it in mcp_supervisor.json. The
    # in-process advisor MUST run under a MockAgentProvider (no real LLM call).
    seen = _capture_provider(monkeypatch)
    monkeypatch.setattr(
        "orcho_mcp.supervisor.paths.resolve_runs_dir", lambda: tmp_path,
    )
    monkeypatch.setattr(
        "orcho_mcp.supervisor.state.read_state", lambda run_dir: {"mock": True},
    )

    request_advice("20260623_120000_aaaaaa")

    from agents.runtimes import MockAgentProvider
    assert isinstance(seen["provider"], MockAgentProvider)


def test_real_run_passes_no_provider(monkeypatch, tmp_path) -> None:
    # A non-mock run leaves provider=None so the SDK builds the real provider.
    seen = _capture_provider(monkeypatch)
    monkeypatch.setattr(
        "orcho_mcp.supervisor.paths.resolve_runs_dir", lambda: tmp_path,
    )
    monkeypatch.setattr(
        "orcho_mcp.supervisor.state.read_state", lambda run_dir: {"mock": False},
    )

    request_advice("20260623_120000_aaaaaa")

    assert seen["provider"] is None


def test_absent_supervisor_state_passes_no_provider(monkeypatch, tmp_path) -> None:
    # No mcp_supervisor.json (e.g. a CLI-launched run): fall back to the real
    # provider, never crash on provider probing.
    seen = _capture_provider(monkeypatch)
    monkeypatch.setattr(
        "orcho_mcp.supervisor.paths.resolve_runs_dir", lambda: tmp_path,
    )
    monkeypatch.setattr(
        "orcho_mcp.supervisor.state.read_state", lambda run_dir: None,
    )

    request_advice("20260623_120000_aaaaaa")

    assert seen["provider"] is None


def test_provider_probe_failure_falls_back_to_none(monkeypatch) -> None:
    # Workspace resolution blowing up must not mask the SDK call: provider
    # probing is best-effort and falls back to None.
    seen = _capture_provider(monkeypatch)

    def _boom() -> object:
        raise RuntimeError("no workspace")

    monkeypatch.setattr(
        "orcho_mcp.supervisor.paths.resolve_runs_dir", _boom,
    )

    request_advice("20260623_120000_aaaaaa")

    assert seen["provider"] is None


# ── SDK error mapping ────────────────────────────────────────────────────────


def test_run_not_found_maps(monkeypatch) -> None:
    def _raise(*a, **k):
        raise _SDKRunNotFound("No run directory: nope")

    monkeypatch.setattr(_SEAM, _raise)
    with pytest.raises(RunNotFoundError):
        request_advice("nope")


def test_no_workspace_maps(monkeypatch) -> None:
    def _raise(*a, **k):
        raise _SDKNoWorkspace("no workspace")

    monkeypatch.setattr(_SEAM, _raise)
    with pytest.raises(WorkspaceNotResolvedError):
        request_advice("20260623_120000_aaaaaa")


def test_invalid_phase_handoff_state_maps_to_invalid_plan(monkeypatch) -> None:
    def _raise(*a, **k):
        raise _SDKInvalidPhaseHandoffState("not eligible for advice")

    monkeypatch.setattr(_SEAM, _raise)
    with pytest.raises(InvalidPlanError):
        request_advice("20260623_120000_aaaaaa")


def test_value_error_maps_to_invalid_plan(monkeypatch) -> None:
    def _raise(*a, **k):
        raise ValueError("run_id must be a non-empty string")

    monkeypatch.setattr(_SEAM, _raise)
    with pytest.raises(InvalidPlanError):
        request_advice("")


# ── L2 registration ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handoff_advice_tool_is_registered_l2() -> None:
    """L2: the tool is registered and visible via in-process list_tools."""
    import orcho_mcp.tools  # noqa: F401 — import wires the @mcp.tool decorators
    from orcho_mcp.instance import mcp

    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert "orcho_handoff_advice" in names
