"""Provider-pressure projection (T1) — single core-typed source + helper.

``project_provider_pressure`` reads ONLY the core-typed errors/halt slice
(``sdk.get_errors_halt`` → ``ErrorsAndHalt.provider_runtime`` /
``ErrorsAndHalt.recovery``) and never parses raw provider output. These tests
pin:

- the ``provider_runtime`` (RateLimit dogfood) mapping — condition present with
  phase / sanitized_message / recoverable;
- a generic errors/halt slice staying generic (``condition_present=False``);
- defensive future-field reads (``pressure_kind`` / ``retry_state`` /
  ``reset_at`` / ``wait_hint``) — ``None`` on today's slice, passed through from
  a stand-in carrying them;
- the separate ``provider_access`` branch (switch-runtime, replacements);
- the single ``build_provider_pressure_next_actions`` helper producing safe,
  typed, feedback-free actions for recoverable / parked / exhausted; and
- the ``build_provider_pressure`` factory producing an identical
  ``.next_actions`` and importable ``ProviderPressure``.
"""
from __future__ import annotations

import pytest

from orcho_mcp.schemas import ProviderPressure
from orcho_mcp.services import run_projection
from orcho_mcp.services.run_projection import (
    ProviderPressureProjection,
    build_provider_pressure,
    build_provider_pressure_next_actions,
    project_provider_pressure,
)

_RUN_ID = "run_pp_1"


class _RuntimeToday:
    """Mirror of today's ``ProviderRuntimeFailure`` — no future attrs."""

    def __init__(
        self,
        *,
        failure_kind: str = "provider_runtime",
        recoverable: bool = True,
        recommended_action: str = "resume_or_retry_phase",
        failed_phase: str = "implement",
        runtime: str = "claude",
        model: str = "claude-opus",
        provider_message: str = "Rate limit reached; retry shortly.",
    ) -> None:
        self.failure_kind = failure_kind
        self.recoverable = recoverable
        self.recommended_action = recommended_action
        self.failed_phase = failed_phase
        self.runtime = runtime
        self.model = model
        self.provider_message = provider_message


class _RuntimeFutureShape(_RuntimeToday):
    """Stand-in carrying the future core fields a fixture supplies."""

    def __init__(
        self,
        *,
        retry_state: str = "parked_until_reset",
        reset_at: str = "2026-06-29T10:00:00Z",
        wait_hint: str = "~30m",
        pressure_kind: str = "rate_limit",
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.retry_state = retry_state
        self.reset_at = reset_at
        self.wait_hint = wait_hint
        self.pressure_kind = pressure_kind


class _RuntimeExhaustedShape(_RuntimeToday):
    """Stand-in for the future exhausted-without-reset shape.

    Core has spent the recoverable budget: ``recoverable=False`` and a
    ``retry_state='exhausted'`` future field, but NO ``reset_at`` — there is
    no reset window to wait for. MCP must read it through without inventing a
    reset time.
    """

    def __init__(
        self,
        *,
        retry_state: str = "exhausted",
        pressure_kind: str = "rate_limit",
        **kwargs: object,
    ) -> None:
        kwargs.setdefault("recoverable", False)
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.retry_state = retry_state
        self.pressure_kind = pressure_kind


class _Replacement:
    def __init__(self, runtime: str, model: str) -> None:
        self.runtime = runtime
        self.model = model


class _AccessRecovery:
    def __init__(self) -> None:
        self.failure_kind = "provider_access"
        self.recoverable = True
        self.recommended_action = "switch_runtime"
        self.failed_phase = "review"
        self.runtime = "claude"
        self.model = "claude-opus"
        self.replacements = (_Replacement("codex", "gpt-5"),)


class _ErrorsAndHalt:
    def __init__(
        self,
        *,
        provider_runtime: object | None = None,
        recovery: object | None = None,
    ) -> None:
        self.status = "failed"
        self.errors = ()
        self.halt_reason = None
        self.halted_at = None
        self.error_summary = None
        self.provider_runtime = provider_runtime
        self.recovery = recovery


def _patch_eh(monkeypatch: pytest.MonkeyPatch, eh: _ErrorsAndHalt) -> None:
    monkeypatch.setattr(
        run_projection, "_sdk_get_errors_halt",
        lambda run_id, cwd=None: eh,
    )


# ── provider_runtime (dogfood RateLimit form) ───────────────────────────────


def test_provider_runtime_condition_present(monkeypatch):
    _patch_eh(monkeypatch, _ErrorsAndHalt(provider_runtime=_RuntimeToday()))

    proj = project_provider_pressure(_RUN_ID)

    assert proj.condition_present is True
    assert proj.source == "provider_runtime"
    assert proj.failure_kind == "provider_runtime"
    assert proj.recoverable is True
    assert proj.phase == "implement"
    assert proj.recommended_action == "resume_or_retry_phase"
    assert proj.sanitized_message == "Rate limit reached; retry shortly."


def test_empty_provider_message_becomes_none(monkeypatch):
    _patch_eh(
        monkeypatch,
        _ErrorsAndHalt(provider_runtime=_RuntimeToday(provider_message="")),
    )

    proj = project_provider_pressure(_RUN_ID)

    assert proj.condition_present is True
    assert proj.sanitized_message is None


# ── generic failure stays generic ───────────────────────────────────────────


def test_no_provider_failure_is_not_present(monkeypatch):
    _patch_eh(monkeypatch, _ErrorsAndHalt())

    proj = project_provider_pressure(_RUN_ID)

    assert proj.condition_present is False
    assert proj.source is None
    assert build_provider_pressure(proj) is None
    assert build_provider_pressure_next_actions(proj) == []


# ── defensive future-field reads ────────────────────────────────────────────


def test_future_fields_none_on_todays_slice(monkeypatch):
    _patch_eh(monkeypatch, _ErrorsAndHalt(provider_runtime=_RuntimeToday()))

    proj = project_provider_pressure(_RUN_ID)

    assert proj.pressure_kind is None
    assert proj.retry_state is None
    assert proj.reset_at is None
    assert proj.wait_hint is None


def test_future_fields_passed_through_from_standin(monkeypatch):
    _patch_eh(
        monkeypatch,
        _ErrorsAndHalt(provider_runtime=_RuntimeFutureShape()),
    )

    proj = project_provider_pressure(_RUN_ID)

    assert proj.pressure_kind == "rate_limit"
    assert proj.retry_state == "parked_until_reset"
    assert proj.reset_at == "2026-06-29T10:00:00Z"
    assert proj.wait_hint == "~30m"


# ── provider_access branch (separate semantics) ─────────────────────────────


def test_provider_access_branch(monkeypatch):
    _patch_eh(monkeypatch, _ErrorsAndHalt(recovery=_AccessRecovery()))

    proj = project_provider_pressure(_RUN_ID)

    assert proj.condition_present is True
    assert proj.source == "provider_access"
    assert proj.failure_kind == "provider_access"
    assert proj.recommended_action == "switch_runtime_or_restore_access"
    assert proj.phase == "review"
    assert proj.replacements == [{"runtime": "codex", "model": "gpt-5"}]
    assert proj.sanitized_message is None


def test_provider_runtime_takes_priority_over_recovery(monkeypatch):
    _patch_eh(
        monkeypatch,
        _ErrorsAndHalt(
            provider_runtime=_RuntimeToday(), recovery=_AccessRecovery(),
        ),
    )

    proj = project_provider_pressure(_RUN_ID)

    assert proj.source == "provider_runtime"


# ── next-actions helper (typed, conservative, feedback-free) ────────────────


def _assert_no_feedback(actions):
    for a in actions:
        assert a.tool != "orcho_phase_handoff_decide"
        assert a.requires_operator_input is False
        assert a.choices is None
        assert a.input_schema is None
        assert "feedback" not in a.args


def test_recoverable_next_actions(monkeypatch):
    _patch_eh(monkeypatch, _ErrorsAndHalt(provider_runtime=_RuntimeToday()))
    proj = project_provider_pressure(_RUN_ID)

    actions = build_provider_pressure_next_actions(proj)

    tools = [a.tool for a in actions]
    assert tools == ["orcho_run_evidence", "orcho_run_resume", "orcho_run_status"]
    resume = next(a for a in actions if a.tool == "orcho_run_resume")
    assert resume.optional is False
    assert resume.args == {"run_id": _RUN_ID}
    _assert_no_feedback(actions)


def test_parked_next_actions(monkeypatch):
    _patch_eh(
        monkeypatch,
        _ErrorsAndHalt(provider_runtime=_RuntimeFutureShape()),
    )
    proj = project_provider_pressure(_RUN_ID)

    actions = build_provider_pressure_next_actions(proj)

    tools = [a.tool for a in actions]
    assert tools == ["orcho_run_status", "orcho_run_resume", "orcho_run_evidence"]
    # The reset time is surfaced as machine-readable context, not invented.
    wait = actions[0]
    assert wait.context == {
        "reset_at": "2026-06-29T10:00:00Z", "wait_hint": "~30m",
    }
    resume = actions[1]
    assert resume.context == {
        "reset_at": "2026-06-29T10:00:00Z", "wait_hint": "~30m",
    }
    _assert_no_feedback(actions)


def test_exhausted_without_reset_next_actions(monkeypatch):
    _patch_eh(
        monkeypatch,
        _ErrorsAndHalt(
            provider_runtime=_RuntimeToday(recoverable=False),
        ),
    )
    proj = project_provider_pressure(_RUN_ID)

    actions = build_provider_pressure_next_actions(proj)

    tools = [a.tool for a in actions]
    assert tools == ["orcho_run_evidence", "orcho_run_resume"]
    resume = next(a for a in actions if a.tool == "orcho_run_resume")
    assert resume.optional is True
    # No reset time fabricated for an exhausted failure.
    assert resume.context is None
    _assert_no_feedback(actions)


def test_exhausted_shape_retry_state_passthrough(monkeypatch):
    # Future exhausted shape: retry_state='exhausted' passes through, NO
    # reset_at is fabricated, and the actions stay the conservative
    # inspect + resume (no wait_until_reset).
    _patch_eh(
        monkeypatch,
        _ErrorsAndHalt(provider_runtime=_RuntimeExhaustedShape()),
    )
    proj = project_provider_pressure(_RUN_ID)

    assert proj.retry_state == "exhausted"
    assert proj.reset_at is None
    assert proj.recoverable is False

    actions = build_provider_pressure_next_actions(proj)
    assert [a.tool for a in actions] == ["orcho_run_evidence", "orcho_run_resume"]
    assert all(a.context is None for a in actions)
    _assert_no_feedback(actions)


def test_provider_access_next_actions_distinct(monkeypatch):
    _patch_eh(monkeypatch, _ErrorsAndHalt(recovery=_AccessRecovery()))
    proj = project_provider_pressure(_RUN_ID)

    actions = build_provider_pressure_next_actions(proj)

    tools = [a.tool for a in actions]
    assert tools == ["orcho_run_evidence", "orcho_run_resume", "orcho_run_status"]
    resume = next(a for a in actions if a.tool == "orcho_run_resume")
    # Access recovery is the restore/switch path — never retry-phase semantics.
    assert "retry the phase" not in resume.intent.lower()
    _assert_no_feedback(actions)


# ── factory parity + importability ──────────────────────────────────────────


def test_factory_matches_helper_next_actions(monkeypatch):
    _patch_eh(monkeypatch, _ErrorsAndHalt(provider_runtime=_RuntimeToday()))
    proj = project_provider_pressure(_RUN_ID)

    model = build_provider_pressure(proj)
    helper = build_provider_pressure_next_actions(proj)

    assert isinstance(model, ProviderPressure)
    assert model.condition == "provider_pressure"
    assert model.failure_kind == "provider_runtime"
    assert model.recoverable is True
    assert model.phase == "implement"
    assert model.sanitized_message == "Rate limit reached; retry shortly."
    assert [a.model_dump() for a in model.next_actions] == [
        a.model_dump() for a in helper
    ]


def test_factory_carries_future_fields(monkeypatch):
    _patch_eh(
        monkeypatch,
        _ErrorsAndHalt(provider_runtime=_RuntimeFutureShape()),
    )
    proj = project_provider_pressure(_RUN_ID)

    model = build_provider_pressure(proj)

    assert model is not None
    assert model.retry_state == "parked_until_reset"
    assert model.reset_at == "2026-06-29T10:00:00Z"
    assert model.wait_hint == "~30m"
    assert model.pressure_kind == "rate_limit"


def test_factory_none_for_absent_projection():
    proj = ProviderPressureProjection(run_id=_RUN_ID, condition_present=False)
    assert build_provider_pressure(proj) is None
    assert build_provider_pressure(None) is None
