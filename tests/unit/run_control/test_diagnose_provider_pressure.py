"""Provider-pressure reconciliation in ``orcho_run_diagnose`` (T2).

``inspect_run_diagnosis`` (the public ``orcho_run_diagnose`` entrypoint) runs a
post-core reconciliation: a residual resumable condition (``halted`` /
``failed`` / ``interrupted``) whose core errors/halt rollup carries a typed
provider runtime/access failure is upgraded to ``condition='provider_pressure'``
with the single shared helper's safe, feedback-free ``next_actions``. These
tests pin:

- the real end-to-end provider_runtime path (today's SDK shape, via a written
  ``meta.failure``);
- a generic failure staying generic (no provider source → no upgrade);
- the guard that NON-residual conditions
  (``needs_decision`` / ``needs_delivery_decision`` / ``superseded_by_child``)
  are never overridden — the reconciliation never even consults provider
  pressure for them;
- the future-shape fixtures (``parked_until_reset`` /
  exhausted-without-reset) the shared helper drives — these go through a
  synthetic projection because core today strips the future fields (documented
  core-blocker).
"""
from __future__ import annotations

import pytest

from orcho_mcp.inspection import diagnosis as diag_mod
from orcho_mcp.inspection.diagnosis import inspect_run_diagnosis
from orcho_mcp.services.run_projection import (
    ProviderPressureProjection,
    RunDiagnosisProjection,
)
from tests.fixtures.mcp_workspace import meta, supervisor_state, write_run

# Today's core ``meta.failure`` shape for a recoverable provider/runtime
# failure: exactly the seven fields ``_provider_runtime_failure_from_meta``
# reads — no future ``pressure_kind`` / ``retry_state`` / ``reset_at`` fields.
_PROVIDER_RUNTIME_FAILURE = {
    "failure_kind": "provider_runtime",
    "recoverable": True,
    "recommended_action": "resume_or_retry_phase",
    "failed_phase": "implement",
    "runtime": "claude",
    "model": "claude-opus",
    "provider_message": "Rate limit reached; retry shortly.",
}


def _assert_no_feedback(actions):
    for a in actions:
        assert a.tool != "orcho_phase_handoff_decide"
        assert a.requires_operator_input is False
        assert "feedback" not in a.args


# ── real end-to-end provider_runtime path ───────────────────────────────────


def test_provider_runtime_failed_run_is_provider_pressure(fake_workspace):
    write_run(
        fake_workspace, "20260101_000010",
        meta=meta(
            status="failed", project="/p/x", task="t",
            failure=dict(_PROVIDER_RUNTIME_FAILURE),
        ),
    )

    d = inspect_run_diagnosis("20260101_000010")

    assert d.condition == "provider_pressure"
    assert d.provider_pressure is not None
    assert d.provider_pressure.recoverable is True
    assert d.provider_pressure.failure_kind == "provider_runtime"
    assert d.provider_pressure.phase == "implement"
    assert d.provider_pressure.sanitized_message == (
        "Rate limit reached; retry shortly."
    )
    # next_actions come from the shared helper (recoverable runtime shape).
    tools = [a.tool for a in d.next_actions]
    assert tools == ["orcho_run_evidence", "orcho_run_resume", "orcho_run_status"]
    # Diagnose surfaces the helper's actions verbatim on the typed field too.
    assert [a.model_dump() for a in d.next_actions] == [
        a.model_dump() for a in d.provider_pressure.next_actions
    ]
    _assert_no_feedback(d.next_actions)
    assert "provider pressure" in d.reason.lower()


# ── generic failure stays generic ───────────────────────────────────────────


def test_generic_failed_run_stays_generic(fake_workspace):
    write_run(
        fake_workspace, "20260101_000011",
        meta=meta(status="failed", project="/p/x", task="t"),
    )

    d = inspect_run_diagnosis("20260101_000011")

    assert d.condition != "provider_pressure"
    assert d.condition in {"failed", "halted", "interrupted"}
    assert d.provider_pressure is None


# ── controllability axis on the diagnose wire (T2) ──────────────────────────


def test_diagnose_wire_control_inspect_only_for_foreign_run(fake_workspace):
    # A foreign / CLI run dir (meta.json only) carries control='inspect_only'
    # on the RunDiagnosis wire model, orthogonal to its resumable condition.
    write_run(
        fake_workspace, "20260101_000020",
        meta=meta(status="failed", project="/p/x", task="t"),
    )

    d = inspect_run_diagnosis("20260101_000020")

    assert d.control == "inspect_only"
    assert d.control_reason and "no mcp_supervisor.json" in d.control_reason


def test_diagnose_wire_control_controllable_for_mcp_started_run(fake_workspace):
    # An MCP-started run (durable mcp_supervisor.json with a project_dir)
    # carries control='mcp_controllable' on the wire model.
    write_run(
        fake_workspace, "20260101_000021",
        meta=meta(status="failed", project="/p/x", task="t"),
        supervisor_state=supervisor_state(
            run_id="20260101_000021", status="failed", project_dir="/p/x",
        ),
    )

    d = inspect_run_diagnosis("20260101_000021")

    assert d.control == "mcp_controllable"
    assert d.control_reason and "project_dir=/p/x" in d.control_reason


# ── non-residual conditions are never overridden ────────────────────────────


def _diag_proj(condition: str, **kw) -> RunDiagnosisProjection:
    return RunDiagnosisProjection(
        condition=condition,
        reason="base reason",
        run_id="r1",
        status=kw.pop("status", "awaiting_phase_handoff"),
        halt_reason=kw.pop("halt_reason", None),
        **kw,
    )


def _present_runtime_pp(run_id: str = "r1") -> ProviderPressureProjection:
    return ProviderPressureProjection(
        run_id=run_id,
        condition_present=True,
        source="provider_runtime",
        failure_kind="provider_runtime",
        recoverable=True,
        recommended_action="resume_or_retry_phase",
        phase="implement",
        sanitized_message="rate limited",
    )


@pytest.mark.parametrize(
    ("condition", "extra"),
    [
        (
            "needs_decision",
            {"handoff_id": "h1", "available_actions": ["continue", "halt"]},
        ),
        (
            "needs_delivery_decision",
            {"delivery_gate_kind": "delivery_decision_required"},
        ),
        ("superseded_by_child", {"recommended_run_id": "child1"}),
    ],
)
def test_non_residual_conditions_not_overridden(monkeypatch, condition, extra):
    monkeypatch.setattr(
        diag_mod, "project_run_diagnosis",
        lambda rid: _diag_proj(condition, **extra),
    )
    consulted = {"n": 0}

    def _pp(rid):
        consulted["n"] += 1
        return _present_runtime_pp(rid)

    monkeypatch.setattr(diag_mod, "project_provider_pressure", _pp)

    d = inspect_run_diagnosis("r1")

    assert d.condition == condition
    assert d.provider_pressure is None
    # The guard short-circuits before consulting provider pressure at all.
    assert consulted["n"] == 0
    assert d.reason == "base reason"


# ── future-shape fixtures driven through the shared helper ───────────────────


def test_parked_until_reset_next_actions(monkeypatch):
    monkeypatch.setattr(
        diag_mod, "project_run_diagnosis",
        lambda rid: _diag_proj("failed", status="failed"),
    )
    monkeypatch.setattr(
        diag_mod, "project_provider_pressure",
        lambda rid: ProviderPressureProjection(
            run_id="r1",
            condition_present=True,
            source="provider_runtime",
            failure_kind="provider_runtime",
            recoverable=True,
            phase="implement",
            retry_state="parked_until_reset",
            reset_at="2026-06-29T10:00:00Z",
            wait_hint="~30m",
        ),
    )

    d = inspect_run_diagnosis("r1")

    assert d.condition == "provider_pressure"
    tools = [a.tool for a in d.next_actions]
    # wait_until_reset → resume_after_reset → inspect_provider_pressure.
    assert tools == [
        "orcho_run_status", "orcho_run_resume", "orcho_run_evidence",
    ]
    assert d.next_actions[0].context == {
        "reset_at": "2026-06-29T10:00:00Z", "wait_hint": "~30m",
    }
    assert d.provider_pressure.retry_state == "parked_until_reset"
    assert d.provider_pressure.reset_at == "2026-06-29T10:00:00Z"
    assert "retry_state parked_until_reset" in d.reason
    _assert_no_feedback(d.next_actions)


def test_exhausted_without_reset_next_actions(monkeypatch):
    monkeypatch.setattr(
        diag_mod, "project_run_diagnosis",
        lambda rid: _diag_proj("failed", status="failed"),
    )
    monkeypatch.setattr(
        diag_mod, "project_provider_pressure",
        lambda rid: ProviderPressureProjection(
            run_id="r1",
            condition_present=True,
            source="provider_runtime",
            failure_kind="provider_runtime",
            recoverable=False,
            phase="implement",
            retry_state="exhausted",
        ),
    )

    d = inspect_run_diagnosis("r1")

    assert d.condition == "provider_pressure"
    tools = [a.tool for a in d.next_actions]
    # inspect + resume, no wait/reset action and no fabricated reset time.
    assert tools == ["orcho_run_evidence", "orcho_run_resume"]
    resume = next(a for a in d.next_actions if a.tool == "orcho_run_resume")
    assert resume.context is None
    assert d.provider_pressure.retry_state == "exhausted"
    assert d.provider_pressure.reset_at is None
    assert "retry_state exhausted" in d.reason
    _assert_no_feedback(d.next_actions)
