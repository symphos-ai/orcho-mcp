"""Unit tests for ``orcho_run_evidence(slice="handoff_advice")``.

Pins the MCP projection layer for the Stage 0/1 handoff-advice evidence slice.
The SDK source of truth (``sdk.list_handoff_advice``, tested in orcho-core) is
monkeypatched at this module's own seam, so these tests cover only the
wire-record mapping: per-call fields (incl. tri-state ``resolved``, usage/cost),
the aggregate summary, the empty/absent surface degrading cleanly (SDK ``None``
and a stale core missing the symbol), and inclusion in ``slice="all"``.
"""

from __future__ import annotations

from orcho_mcp.inspection.evidence import inspect_run_evidence

_SEAM = "orcho_mcp.inspection.evidence._sdk_list_handoff_advice"


def _call(**kw):
    """Build an SDK HandoffAdviceCall with sensible defaults."""
    from sdk import HandoffAdviceCall

    base = dict(
        handoff_id="review_changes:repair_round:1",
        phase="review_changes",
        advice_artifact="phase_handoff_advice/h1.json",
        trigger="rejected",
        verdict="REJECTED",
        feedback_source="agent_advice",
        recommended_action="retry_feedback",
        applied_action="retry_feedback",
        confidence="high",
        finding_fingerprint="F1|P1|bug",
        resolved=True,
        repeated=False,
        outcome="resolved",
        severity_counts={"P1": 1},
        tokens_in=100,
        tokens_out=50,
        tokens_cached=3,
        duration_s=4.5,
        cost_usd_equivalent=None,
        model=None,
    )
    base.update(kw)
    return HandoffAdviceCall(**base)


def _usage(**kw):
    from sdk import HandoffAdviceUsage

    base = dict(
        tokens_in=100,
        tokens_out=50,
        tokens_cached=3,
        duration_s=4.5,
        cost_usd_equivalent=None,
    )
    base.update(kw)
    return HandoffAdviceUsage(**base)


def _summary(**kw):
    from sdk import HandoffAdviceSummary

    base = dict(
        calls=1,
        applied_retries=1,
        resolved_retries=1,
        repeated=0,
        stopped=0,
        unknown=0,
        usage=_usage(),
    )
    base.update(kw)
    return HandoffAdviceSummary(**base)


def _evidence(calls=None, summary=None):
    from sdk import HandoffAdviceEvidence

    return HandoffAdviceEvidence(
        calls=tuple(calls if calls is not None else (_call(),)),
        summary=summary if summary is not None else _summary(),
    )


def _patch(monkeypatch, evidence):
    monkeypatch.setattr(_SEAM, lambda *a, **k: evidence)


# ── per-call + summary projection ────────────────────────────────────────────


def test_handoff_advice_projects_calls_and_summary(monkeypatch) -> None:
    _patch(monkeypatch, _evidence())

    result = inspect_run_evidence("rid", slice="handoff_advice")

    assert result.handoff_advice is not None
    assert len(result.handoff_advice.calls) == 1
    call = result.handoff_advice.calls[0]
    assert call.handoff_id == "review_changes:repair_round:1"
    assert call.phase == "review_changes"
    assert call.advice_artifact == "phase_handoff_advice/h1.json"
    assert call.recommended_action == "retry_feedback"
    assert call.applied_action == "retry_feedback"
    assert call.confidence == "high"
    assert call.resolved is True
    assert call.repeated is False
    assert call.outcome == "resolved"
    assert call.finding_fingerprint == "F1|P1|bug"
    assert call.severity_counts == {"P1": 1}
    # Usage + cost preserved per-call (cost None — never fabricated).
    assert call.tokens_in == 100
    assert call.tokens_out == 50
    assert call.tokens_cached == 3
    assert call.duration_s == 4.5
    assert call.cost_usd_equivalent is None
    assert call.model is None

    summary = result.handoff_advice.summary
    assert summary.calls == 1
    assert summary.applied_retries == 1
    assert summary.resolved_retries == 1
    assert summary.repeated == 0
    assert summary.stopped == 0
    assert summary.unknown == 0
    assert summary.usage is not None
    assert summary.usage.tokens_in == 100
    assert summary.usage.tokens_cached == 3
    assert summary.usage.duration_s == 4.5
    assert summary.usage.cost_usd_equivalent is None


def test_handoff_advice_tri_state_resolved_and_stopped(monkeypatch) -> None:
    # An unapplied advisory call: applied=None, resolved=None, outcome=stopped.
    call = _call(
        applied_action=None,
        feedback_source=None,
        resolved=None,
        repeated=False,
        outcome="stopped",
    )
    summary = _summary(
        calls=1,
        applied_retries=0,
        resolved_retries=0,
        stopped=1,
        usage=None,
    )
    _patch(monkeypatch, _evidence(calls=(call,), summary=summary))

    result = inspect_run_evidence("rid", slice="handoff_advice")
    out_call = result.handoff_advice.calls[0]
    assert out_call.applied_action is None
    assert out_call.feedback_source is None
    assert out_call.resolved is None
    assert out_call.outcome == "stopped"
    assert result.handoff_advice.summary.stopped == 1
    assert result.handoff_advice.summary.usage is None


# ── absent surface degrades cleanly ──────────────────────────────────────────


def test_handoff_advice_none_from_sdk_is_empty_slice(monkeypatch) -> None:
    _patch(monkeypatch, None)

    result = inspect_run_evidence("rid", slice="handoff_advice")

    assert result.handoff_advice is not None
    assert result.handoff_advice.calls == []
    assert result.handoff_advice.summary.calls == 0
    assert result.handoff_advice.summary.applied_retries == 0
    assert result.handoff_advice.summary.usage is None


def test_handoff_advice_stale_core_missing_symbol_is_empty_slice(
    monkeypatch,
) -> None:
    # A version-skewed core that predates the projection: the seam is None.
    # The slice must still serve a clean empty result, never raise.
    monkeypatch.setattr(_SEAM, None)

    result = inspect_run_evidence("rid", slice="handoff_advice")

    assert result.handoff_advice is not None
    assert result.handoff_advice.calls == []
    assert result.handoff_advice.summary.calls == 0


# ── slice isolation + inclusion in slice="all" ───────────────────────────────


def test_handoff_advice_slice_isolates_other_slices(monkeypatch) -> None:
    _patch(monkeypatch, _evidence())

    result = inspect_run_evidence("rid", slice="handoff_advice")
    assert result.handoff_advice is not None
    assert result.plan is None
    assert result.findings is None
    assert result.verification_timeline is None


def test_all_slice_includes_handoff_advice(monkeypatch) -> None:
    from sdk import ErrorsAndHalt, PlanSummary
    from sdk.verification_timeline import VerificationTimelineProjection

    import orcho_mcp.inspection.evidence as ev

    _patch(monkeypatch, _evidence())
    # Stub every other SDK seam so slice="all" does not touch a real run.
    monkeypatch.setattr(
        ev,
        "_sdk_get_verification_timeline",
        lambda *, run_id, **_: VerificationTimelineProjection(
            schema_version="1",
            run_id="rid",
            project="/proj",
        ),
    )
    monkeypatch.setattr(
        ev,
        "_sdk_get_plan_summary",
        lambda *a, **k: PlanSummary(
            source="plan",
            short_summary="",
            planning_context="",
            subtask_count=0,
            has_contract=False,
            goal="",
            acceptance_criteria=(),
            owned_files=(),
            commands_to_run=(),
            risks=(),
            review_focus=(),
        ),
    )
    monkeypatch.setattr(ev, "_sdk_list_findings", lambda *a, **k: [])
    monkeypatch.setattr(ev, "_sdk_list_evidence_commands", lambda *a, **k: [])
    monkeypatch.setattr(ev, "_sdk_list_evidence_artifacts", lambda *a, **k: [])
    monkeypatch.setattr(
        ev,
        "_sdk_get_errors_halt",
        lambda *a, **k: ErrorsAndHalt(
            status="done",
            errors=(),
            halt_reason=None,
            halted_at=None,
            error_summary=None,
        ),
    )
    monkeypatch.setattr(ev, "_sdk_list_sub_runs", lambda *a, **k: [])
    monkeypatch.setattr(ev, "_sdk_list_subtask_receipts", lambda *a, **k: [])
    monkeypatch.setattr(ev, "_read_verification_receipts", lambda run_dir: [])
    monkeypatch.setattr(
        ev,
        "find_run_dir",
        lambda run_id: __import__("pathlib").Path("/tmp"),
    )

    result = inspect_run_evidence("rid", slice="all")
    assert result.handoff_advice is not None
    assert len(result.handoff_advice.calls) == 1
    assert result.handoff_advice.calls[0].outcome == "resolved"
    assert result.handoff_advice.summary.resolved_retries == 1
