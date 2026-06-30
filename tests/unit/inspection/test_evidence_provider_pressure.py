"""Evidence ``errors`` slice carries the typed provider-pressure (T3).

The ``errors`` slice of ``inspect_run_evidence`` must surface the core-typed
provider runtime/access failure (``ErrorsHaltSliceRecord.provider_pressure``)
projected from the same ``project_provider_pressure`` source as the other
surfaces — so the core fact is never lost on the evidence path. This pins the
slice's own shape (phase / recoverable / sanitized_message / next_actions); the
cross-surface equality lives in ``observe/test_provider_pressure_status.py``.
"""
from __future__ import annotations

from orcho_mcp.inspection.evidence import inspect_run_evidence
from tests.fixtures.mcp_workspace import meta, write_run

_PROVIDER_RUNTIME_FAILURE = {
    "failure_kind": "provider_runtime",
    "recoverable": True,
    "recommended_action": "resume_or_retry_phase",
    "failed_phase": "implement",
    "runtime": "claude",
    "model": "claude-opus",
    "provider_message": "Rate limit reached; retry shortly.",
}


def test_errors_slice_carries_provider_pressure(fake_workspace):
    write_run(
        fake_workspace, "20260101_000030",
        meta=meta(
            status="failed", project="/p/x", task="t",
            failure=dict(_PROVIDER_RUNTIME_FAILURE),
        ),
    )

    errors = inspect_run_evidence("20260101_000030", slice="errors").errors
    pp = errors.provider_pressure

    assert pp is not None
    assert pp.condition == "provider_pressure"
    assert pp.failure_kind == "provider_runtime"
    assert pp.recoverable is True
    assert pp.phase == "implement"
    assert pp.sanitized_message == "Rate limit reached; retry shortly."
    # Typed conservative next_actions from the shared helper, feedback-free.
    assert [a.tool for a in pp.next_actions] == [
        "orcho_run_evidence", "orcho_run_resume", "orcho_run_status",
    ]
    for a in pp.next_actions:
        assert a.tool != "orcho_phase_handoff_decide"
        assert "feedback" not in a.args


def test_provider_access_slice_shape(fake_workspace):
    write_run(
        fake_workspace, "20260101_000031",
        meta=meta(
            status="failed", project="/p/x", task="t",
            failure={
                "failure_kind": "provider_access",
                "recoverable": True,
                "recommended_action": "switch_runtime",
                "failed_phase": "review",
                "runtime": "claude",
                "model": "claude-opus",
                "recovery_actions": [
                    {"action": "replace", "runtime": "codex", "model": "gpt-5"},
                ],
            },
        ),
    )

    pp = inspect_run_evidence("20260101_000031", slice="errors").errors.provider_pressure

    assert pp is not None
    assert pp.failure_kind == "provider_access"
    # Projection stamps the distinct access recommended action.
    assert pp.recommended_action == "switch_runtime_or_restore_access"
    assert pp.phase == "review"


def test_generic_failure_has_no_provider_pressure(fake_workspace):
    write_run(
        fake_workspace, "20260101_000032",
        meta=meta(status="failed", project="/p/x", task="t"),
    )

    pp = inspect_run_evidence("20260101_000032", slice="errors").errors.provider_pressure

    assert pp is None
