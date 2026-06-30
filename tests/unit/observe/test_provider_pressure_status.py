"""AC7 cross-surface provider-pressure consistency (T3).

The same ``project_provider_pressure`` source and the single shared
``build_provider_pressure_next_actions`` helper feed FOUR surfaces:

- status   — ``RunStatus`` from ``services.run_reads.get_run_status``;
- evidence — ``ErrorsHaltSliceRecord.provider_pressure`` from the ``errors``
  slice of ``inspection.evidence.inspect_run_evidence``;
- diagnose — ``RunDiagnosis.provider_pressure`` from
  ``inspection.diagnosis.inspect_run_diagnosis``;
- summary  — ``RunEventsSummary.provider_pressure`` from
  ``observe.summary.build_run_events_summary``.

For one provider_runtime run these four MUST agree on ``condition`` /
``failure_kind`` / ``phase`` / ``next_actions`` (a typed
``list[NextActionRecord]``). For a generic failure they MUST all be ``None``.
The live-status card (a 5th surface) is asserted to carry the typed condition
too, with a resume-later/inspect ``next_action`` — never a review / delivery /
operator-halt rejection.
"""
from __future__ import annotations

from orcho_mcp.inspection.diagnosis import inspect_run_diagnosis
from orcho_mcp.inspection.evidence import inspect_run_evidence
from orcho_mcp.observe.live_status import build_run_live_status
from orcho_mcp.observe.summary import build_run_events_summary
from orcho_mcp.services.run_reads import get_run_status
from tests.fixtures.mcp_workspace import meta, write_run

# Today's core ``meta.failure`` shape for a recoverable provider/runtime
# failure — exactly the seven fields the SDK errors/halt projection reads.
_PROVIDER_RUNTIME_FAILURE = {
    "failure_kind": "provider_runtime",
    "recoverable": True,
    "recommended_action": "resume_or_retry_phase",
    "failed_phase": "implement",
    "runtime": "claude",
    "model": "claude-opus",
    "provider_message": "Rate limit reached; retry shortly.",
}


def _four_surfaces(run_id):
    status = get_run_status(run_id).provider_pressure
    evidence = inspect_run_evidence(run_id, slice="errors").errors.provider_pressure
    diagnose = inspect_run_diagnosis(run_id).provider_pressure
    summary = build_run_events_summary(run_id).provider_pressure
    return status, evidence, diagnose, summary


def test_provider_pressure_consistent_across_four_surfaces(fake_workspace):
    write_run(
        fake_workspace, "20260101_000020",
        meta=meta(
            status="failed", project="/p/x", task="t",
            failure=dict(_PROVIDER_RUNTIME_FAILURE),
        ),
    )

    status, evidence, diagnose, summary = _four_surfaces("20260101_000020")

    surfaces = [status, evidence, diagnose, summary]
    # Every surface carries the typed condition — evidence included (not a
    # presence flag).
    assert all(pp is not None for pp in surfaces)

    # Full equality on the AC7 fields across all four surfaces.
    conditions = {pp.condition for pp in surfaces}
    assert conditions == {"provider_pressure"}
    failure_kinds = {pp.failure_kind for pp in surfaces}
    assert failure_kinds == {"provider_runtime"}
    phases = {pp.phase for pp in surfaces}
    assert phases == {"implement"}

    # next_actions are a typed list[NextActionRecord] from the ONE shared
    # helper — byte-identical across surfaces.
    dumps = [
        [a.model_dump() for a in pp.next_actions] for pp in surfaces
    ]
    assert dumps[0] == dumps[1] == dumps[2] == dumps[3]
    # Sanity: the recoverable runtime shape, feedback-free.
    assert [a.tool for a in status.next_actions] == [
        "orcho_run_evidence", "orcho_run_resume", "orcho_run_status",
    ]
    for a in status.next_actions:
        assert a.tool != "orcho_phase_handoff_decide"
        assert "feedback" not in a.args


def test_live_status_card_carries_provider_pressure(fake_workspace):
    write_run(
        fake_workspace, "20260101_000021",
        meta=meta(
            status="failed", project="/p/x", task="t",
            failure=dict(_PROVIDER_RUNTIME_FAILURE),
        ),
    )

    card = build_run_live_status("20260101_000021")

    assert card.provider_pressure is not None
    assert card.provider_pressure.failure_kind == "provider_runtime"
    # The card must read as resume-later/inspect, never an ordinary rejection.
    na = card.next_action.lower()
    assert "provider under pressure" in na
    assert "orcho_run_resume" in na
    for forbidden in ("final acceptance", "review", "operator"):
        assert forbidden not in na


def test_generic_failure_provider_pressure_none_on_all_surfaces(fake_workspace):
    write_run(
        fake_workspace, "20260101_000022",
        meta=meta(status="failed", project="/p/x", task="t"),
    )

    status, evidence, diagnose, summary = _four_surfaces("20260101_000022")

    assert status is None
    assert evidence is None
    assert diagnose is None
    assert summary is None
    # The live-status card is also clean for a generic failure.
    card = build_run_live_status("20260101_000022")
    assert card.provider_pressure is None


def test_legacy_summary_next_actions_unbroken(fake_workspace):
    """The legacy ``next_actions: list[str]`` stays the status-derived strings;
    provider-pressure actions live only in ``provider_pressure.next_actions``."""
    write_run(
        fake_workspace, "20260101_000023",
        meta=meta(
            status="failed", project="/p/x", task="t",
            failure=dict(_PROVIDER_RUNTIME_FAILURE),
        ),
    )

    summary = build_run_events_summary("20260101_000023")

    # Still the plain list[str] terminal-failure guidance, not typed records.
    assert summary.next_actions == [
        "inspect orcho_run_evidence for errors",
        "inspect orcho_run_metrics for what completed",
    ]
    assert all(isinstance(s, str) for s in summary.next_actions)
    assert summary.provider_pressure is not None
