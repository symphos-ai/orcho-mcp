"""orcho_mcp.inspection.evidence — typed evidence-slice projections.

Sync public function ``inspect_run_evidence`` backs the
``orcho_run_evidence`` MCP tool. Surfaces narrow projections control-loop
clients actually need (plan summary, filtered findings, commands,
artifacts, errors/halt, sub-run links) instead of the full
``collect_evidence`` bundle.

SDK aliases (``_sdk_get_plan_summary``, ``_sdk_list_findings``,
``_sdk_list_evidence_commands``, ``_sdk_list_evidence_artifacts``,
``_sdk_get_errors_halt``, ``_sdk_list_sub_runs``) live in this module
so the MCP adapter layer does not call the SDK directly.
``_sdk_list_findings`` is also imported by
``orcho_mcp.observe.handoff_hints`` for the paused-run handoff hint
builder; the two modules each own their own monkeypatch entry point
(intentional duplication for isolated tests).

The ``verification_receipts`` slice is the one read that does not go
through an SDK projection: the SDK exposes only a thin summary of the
verification-environment receipts (no interpreter / cwd / per-check /
per-command detail), so this module reads the durable JSON artifacts
under ``<run_dir>/verification_receipts/`` directly — the sanctioned
fallback — without re-implementing the banned generic loaders.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sdk import (
    get_errors_halt as _sdk_get_errors_halt,
    get_plan_summary as _sdk_get_plan_summary,
    list_evidence_artifacts as _sdk_list_evidence_artifacts,
    list_evidence_commands as _sdk_list_evidence_commands,
    list_findings as _sdk_list_findings,
    list_sub_runs as _sdk_list_sub_runs,
    list_subtask_receipts as _sdk_list_subtask_receipts,
)

# ``get_verification_timeline`` is a newer SDK projection (the durable
# verification-gate timeline). Import it DEFENSIVELY: a version-skewed or
# stale ``orcho-core`` that predates it must not break module import, which
# would take down EVERY ``orcho_run_evidence`` slice, not just the new one.
# When the symbol is absent the module still loads, all existing slices keep
# working, ``slice="all"`` simply omits ``verification_timeline``, and an
# explicit ``slice="verification_timeline"`` request raises a clear, mapped
# error pointing at the core-version requirement. Under a correctly connected
# editable core this binds to the real SDK function — unchanged behaviour.
try:
    from sdk import get_verification_timeline as _sdk_get_verification_timeline
except ImportError:  # pragma: no cover - exercised by the stale-core unit test
    _sdk_get_verification_timeline = None

# ``list_handoff_advice`` is a newer SDK projection (Stage 0/1 handoff-advice
# evidence). Import it DEFENSIVELY for the same reason as the timeline above: a
# version-skewed core that predates it must not break module import (which would
# take down EVERY ``orcho_run_evidence`` slice). When absent the slice degrades
# to a clean empty result rather than raising — handoff_advice is a purely
# additive, non-required slice, so ``slice="all"`` simply surfaces it empty.
try:
    from sdk import list_handoff_advice as _sdk_list_handoff_advice
except ImportError:  # pragma: no cover - exercised by a stale-core unit test
    _sdk_list_handoff_advice = None

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.inspection.verification_cockpit import build_verification_cockpit
from orcho_mcp.schemas import (
    CriterionReportRecord,
    ErrorsHaltSliceRecord,
    EvidenceArtifactSliceRecord,
    EvidenceCommandSliceRecord,
    EvidenceResult,
    FindingRecord,
    HandoffAdviceCallRecord,
    HandoffAdviceSliceRecord,
    HandoffAdviceSummaryRecord,
    HandoffAdviceUsageRecord,
    ImplementDeliveryRecord,
    PlanSliceRecord,
    SubRunLinkRecord,
    SubtaskReceiptRecord,
    VerificationAutorunEventRecord,
    VerificationCheckRecord,
    VerificationCommandRecord,
    VerificationReceiptRecord,
    VerificationTimelineGateRecord,
    VerificationTimelineRecord,
)
from orcho_mcp.services.errors import map_sdk_errors
from orcho_mcp.services.run_lookup import find_run_dir
from orcho_mcp.services.run_projection import (
    build_provider_pressure,
    project_provider_pressure_from_errors_halt,
)

# Verification-environment receipts live under
# ``<run_dir>/verification_receipts/<phase>_round<N>.json``. The SDK
# evidence bundle only carries a thin *summary* (phase/round/check-counts),
# not the interpreter / cwd / per-check / per-command detail the operator
# needs — and there is no SDK reader for the full receipt (T0 audit). So
# this slice reads the durable JSON artifacts directly (the sanctioned T0
# fallback), defensively, without re-implementing the banned generic
# ``_load_json`` / ``_load_meta`` helpers.
_VERIFICATION_RECEIPTS_DIRNAME = "verification_receipts"


def _optional_str(value: Any) -> str | None:
    """Coerce a breadcrumb field to a non-empty str, else ``None``."""
    if value is None:
        return None
    s = str(value)
    return s or None


def _str_list(value: Any) -> list[str]:
    """Coerce a breadcrumb list field to ``list[str]``; non-lists → ``[]``."""
    if not isinstance(value, list):
        return []
    return [str(x) for x in value if x is not None]


def _project_implement_delivery(
    errors: list[dict[str, Any]],
) -> ImplementDeliveryRecord | None:
    """Project the implement delivery/waiver audit from the errors-rollup.

    The typed delivery/waiver projection lives on the ``errors`` slice. The
    single source is the already-fetched ``errors`` list (the result of
    ``_sdk_get_errors_halt`` — the very same rollup the raw ``errors``
    field surfaces), so the typed record can never drift from the raw
    breadcrumbs. This deliberately does NOT re-read meta; ``RunStatus.meta``
    carries the same scalar audit fields in its summary projection.

    Merges two rollup breadcrumb dicts:
      - ``kind == 'implement_delivery'`` → delivery_status, delivery_waived,
        waiver_id, action, incomplete_subtasks, missing_subtask_receipts;
      - ``kind == 'phase_handoff_waiver'`` → decided_by.

    Returns ``None`` for a clean delivery — i.e. when the rollup carries no
    ``implement_delivery`` breadcrumb.
    """
    delivery: dict[str, Any] | None = None
    waiver: dict[str, Any] | None = None
    for e in errors:
        if not isinstance(e, dict):
            continue
        kind = e.get("kind")
        if kind == "implement_delivery" and delivery is None:
            delivery = e
        elif kind == "phase_handoff_waiver" and waiver is None:
            waiver = e
    if delivery is None:
        return None

    # ``decided_by`` is owned by the phase_handoff_waiver breadcrumb;
    # fall back to the delivery breadcrumb (auto path may stamp it there).
    decided_by = _optional_str(waiver.get("decided_by")) if waiver else None
    if decided_by is None:
        decided_by = _optional_str(delivery.get("decided_by"))

    raw_status = delivery.get("delivery_status")
    delivery_status = str(raw_status) if raw_status is not None else "incomplete"

    return ImplementDeliveryRecord(
        delivery_status=delivery_status,
        delivery_waived=bool(delivery.get("delivery_waived")),
        waiver_id=_optional_str(delivery.get("waiver_id")),
        action=_optional_str(delivery.get("action")),
        decided_by=decided_by,
        incomplete_subtasks=_str_list(delivery.get("incomplete_subtasks")),
        missing_subtask_receipts=_str_list(
            delivery.get("missing_subtask_receipts"),
        ),
    )


def _coerce_argv(raw: Any) -> list[str]:
    """Normalise a recorded ``argv`` to ``list[str]``.

    Core's receipt writer stores ``argv`` as either a list or a string;
    anything else coerces to ``[]``.
    """
    if isinstance(raw, list):
        return [str(a) for a in raw]
    if isinstance(raw, str) and raw:
        return [raw]
    return []


def _coerce_exit_code(raw: Any) -> int | None:
    """An ``int`` exit code, rejecting bool; ``None`` otherwise."""
    if isinstance(raw, bool):
        return None
    return raw if isinstance(raw, int) else None


def _project_verification_receipt(
    data: dict[str, Any], artifact_path: Path,
) -> VerificationReceiptRecord:
    """Project one parsed receipt dict into a typed record.

    Defensive: malformed members are coerced/skipped, never raised. The
    ``artifact_path`` is the on-disk receipt under the run directory (an
    orcho-owned artifact — safe to surface).
    """
    raw_checks = data.get("checks")
    checks: list[VerificationCheckRecord] = []
    for c in raw_checks if isinstance(raw_checks, list) else []:
        if not isinstance(c, dict):
            continue
        checks.append(VerificationCheckRecord(
            name=str(c.get("name", "")),
            expected=_optional_str(c.get("expected")),
            actual=_optional_str(c.get("actual")),
            passed=bool(c.get("passed")),
        ))

    raw_commands = data.get("commands")
    commands: list[VerificationCommandRecord] = []
    for cm in raw_commands if isinstance(raw_commands, list) else []:
        if not isinstance(cm, dict):
            continue
        commands.append(VerificationCommandRecord(
            argv=_coerce_argv(cm.get("argv")),
            exit_code=_coerce_exit_code(cm.get("exit_code")),
        ))

    all_passed = bool(checks) and all(c.passed for c in checks)
    raw_round = data.get("round")
    round_val = raw_round if isinstance(raw_round, int) and not isinstance(
        raw_round, bool,
    ) else None

    return VerificationReceiptRecord(
        phase=_optional_str(data.get("phase")),
        round=round_val,
        kind=str(data.get("kind") or "verification_environment"),
        python=_optional_str(data.get("python")),
        cwd=_optional_str(data.get("cwd")),
        checks=checks,
        commands=commands,
        temp_env_outside_checkout=bool(
            data.get("temp_env_outside_checkout", True),
        ),
        all_passed=all_passed,
        artifact_path=str(artifact_path),
    )


def _read_verification_receipts(
    run_dir: Path,
) -> list[VerificationReceiptRecord]:
    """Read + project every receipt under ``<run_dir>/verification_receipts/``.

    Tolerant: returns ``[]`` when the directory is absent and skips
    unreadable / malformed / non-dict files. Sorted by ``(phase, round)``
    to match orcho-core's own ``load_verification_receipts`` ordering.
    """
    receipts_dir = run_dir / _VERIFICATION_RECEIPTS_DIRNAME
    if not receipts_dir.is_dir():
        return []
    out: list[VerificationReceiptRecord] = []
    for entry in sorted(receipts_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        out.append(_project_verification_receipt(data, entry))
    out.sort(key=lambda r: (r.phase or "", r.round or 0))
    return out


def _project_verification_timeline(proj: Any) -> VerificationTimelineRecord:
    """Project the SDK verification-timeline dataclass into the wire record.

    Pure pass-through: the SDK
    (``sdk.get_verification_timeline``) already owns the durable
    classification and the six-value status enum (no ``MANUAL``), so this
    only maps the typed dataclass onto the Pydantic wire model — per-gate
    ``searched_run_dirs`` / ``rerun_hint`` / ``status`` included. Empty SDK
    strings (``env`` / ``hook`` / ``receipt_path`` / ``source_run_id`` /
    ``stale_reason``) collapse to ``None`` so the wire stays clean. The SDK
    gate record carries no per-gate ``source``; it is reported ``None``.
    """
    gates = [
        VerificationTimelineGateRecord(
            command=g.command,
            env=_optional_str(g.env),
            hook=_optional_str(g.hook),
            source=None,
            policy=_optional_str(g.policy),
            required=bool(g.required),
            status=g.status,
            receipt_path=_optional_str(g.receipt_path),
            source_run_id=_optional_str(g.source_run_id),
            inherited=bool(g.inherited),
            stale_reason=_optional_str(g.stale_reason),
            searched_run_dirs=[str(d) for d in g.searched_run_dirs],
            rerun_hint=[str(h) for h in g.rerun_hint],
            # Mirror the SDK gate's environment-provenance note: a provenance
            # break downgrades the gate to FAIL and populates this. The
            # ``getattr`` default keeps a version-skewed core without the field
            # from breaking the slice.
            detail=_optional_str(getattr(g, "detail", None)),
        )
        for g in proj.gates
    ]
    autorun_events = [
        VerificationAutorunEventRecord(
            hook_label=str(e.hook_label),
            source=str(e.source),
            ran_pass=[str(c) for c in e.ran_pass],
            ran_fail=[str(c) for c in e.ran_fail],
            skipped_fresh=[str(c) for c in e.skipped_fresh],
            skipped_manual=[str(c) for c in e.skipped_manual],
            receipt_paths=[str(p) for p in e.receipt_paths],
        )
        for e in proj.autorun_events
    ]
    return VerificationTimelineRecord(
        run_id=str(proj.run_id),
        has_contract=bool(proj.has_contract),
        gates=gates,
        residual_missing=[str(c) for c in proj.residual_missing],
        residual_stale=[str(c) for c in proj.residual_stale],
        residual_failed=[str(c) for c in proj.residual_failed],
        manual_only=[str(c) for c in proj.manual_only],
        inherited=[str(c) for c in proj.inherited],
        searched_run_dirs=[str(d) for d in proj.searched_run_dirs],
        suggested_commands=[str(c) for c in proj.suggested_commands],
        autorun_events=autorun_events,
        scheduled_trail_available=bool(proj.scheduled_trail_available),
    )


def _project_handoff_advice(ev: Any) -> HandoffAdviceSliceRecord:
    """Project the SDK ``HandoffAdviceEvidence`` dataclass into the wire record.

    Pure pass-through: the SDK projection (itself a verbatim wrapper over
    ``collect_handoff_advice``) owns the outcome classification; this only maps
    the typed dataclass onto the Pydantic wire model. ``ev is None`` (the SDK's
    "no Stage 0/1 advisor surface" signal) yields a clean empty slice — never an
    exception — so an advice-less run reports empty, not absent-by-error.
    """
    if ev is None:
        return HandoffAdviceSliceRecord()

    calls = [
        HandoffAdviceCallRecord(
            handoff_id=c.handoff_id,
            phase=c.phase,
            advice_artifact=c.advice_artifact,
            trigger=c.trigger,
            verdict=c.verdict,
            feedback_source=c.feedback_source,
            recommended_action=c.recommended_action,
            applied_action=c.applied_action,
            confidence=c.confidence,
            finding_fingerprint=c.finding_fingerprint,
            resolved=c.resolved,
            repeated=c.repeated,
            outcome=c.outcome,
            severity_counts=dict(c.severity_counts),
            tokens_in=c.tokens_in,
            tokens_out=c.tokens_out,
            tokens_cached=c.tokens_cached,
            duration_s=c.duration_s,
            cost_usd_equivalent=c.cost_usd_equivalent,
            model=c.model,
        )
        for c in ev.calls
    ]

    s = ev.summary
    usage = None
    if s.usage is not None:
        usage = HandoffAdviceUsageRecord(
            tokens_in=s.usage.tokens_in,
            tokens_out=s.usage.tokens_out,
            tokens_cached=s.usage.tokens_cached,
            duration_s=s.usage.duration_s,
            cost_usd_equivalent=s.usage.cost_usd_equivalent,
        )
    summary = HandoffAdviceSummaryRecord(
        calls=s.calls,
        applied_retries=s.applied_retries,
        resolved_retries=s.resolved_retries,
        repeated=s.repeated,
        stopped=s.stopped,
        unknown=s.unknown,
        usage=usage,
    )
    return HandoffAdviceSliceRecord(calls=calls, summary=summary)


def inspect_run_evidence(
    run_id: str,
    slice: str = "all",
    severity_min: str | None = None,
    phases: list[str] | None = None,
) -> EvidenceResult:
    """Inspect a run via typed slices — no raw log scraping required.

    See ``orcho_run_evidence`` docstring in ``orcho_mcp.tools`` for the
    wire contract. This module owns the implementation; the tool is a
    thin sync shim.
    """
    valid_slices = {
        "all", "plan", "findings", "commands", "artifacts",
        "errors", "sub_runs", "receipts", "verification_receipts",
        "verification_timeline", "verification_cockpit", "handoff_advice",
    }
    if slice not in valid_slices:
        raise InvalidPlanError(
            f"orcho_run_evidence: slice must be one of "
            f"{sorted(valid_slices)!r}, got {slice!r}"
        )

    sev_kw: dict[str, Any] = {}
    if severity_min is not None:
        if severity_min not in ("P0", "P1", "P2", "P3"):
            raise InvalidPlanError(
                f"orcho_run_evidence: severity_min must be one of "
                f"('P0', 'P1', 'P2', 'P3'), got {severity_min!r}"
            )
        sev_kw["severity_min"] = severity_min
    phases_kw = tuple(phases) if phases else None

    want = {slice} if slice != "all" else valid_slices - {"all"}
    out: dict[str, Any] = {"run_id": run_id, "slice": slice}

    # Capability precondition (deterministic, slice-order-independent): the
    # ``verification_timeline`` AND the derived ``verification_cockpit`` slices
    # both need an orcho-core that exports ``sdk.get_verification_timeline`` —
    # the cockpit is projected from the very same SDK call. Both are REQUIRED
    # slices of ``slice="all"``, so whether either was requested explicitly or
    # via ``all``, a too-old/stale core must fail loud here — never silently drop
    # it from the bundle (that would make ``all`` lie about completeness). The
    # defensive import keeps the module loadable so a slice that needs neither
    # still serves.
    needs_timeline = bool(want & {"verification_timeline", "verification_cockpit"})
    if needs_timeline and _sdk_get_verification_timeline is None:
        raise InvalidPlanError(
            "orcho_run_evidence: slices 'verification_timeline' / "
            "'verification_cockpit' require an orcho-core that exposes "
            "sdk.get_verification_timeline; the connected core is too old/stale. "
            "Point orcho-mcp at a core build that includes the "
            "verification-timeline projection."
        )

    with map_sdk_errors(run_id):
        if "plan" in want:
            p = _sdk_get_plan_summary(run_id, cwd=None)
            out["plan"] = PlanSliceRecord(
                source=p.source,
                short_summary=p.short_summary,
                planning_context=p.planning_context,
                subtask_count=p.subtask_count,
                has_contract=p.has_contract,
                goal=p.goal,
                acceptance_criteria=list(p.acceptance_criteria),
                owned_files=list(p.owned_files),
                commands_to_run=list(p.commands_to_run),
                risks=list(p.risks),
                review_focus=list(p.review_focus),
            )

        if "findings" in want:
            findings = _sdk_list_findings(
                run_id, cwd=None, phases=phases_kw, **sev_kw,
            )
            out["findings"] = [
                FindingRecord(
                    id=f.id, severity=f.severity, title=f.title,
                    body=f.body, required_fix=f.required_fix,
                    file=f.file, line=f.line, phase=f.phase,
                    attempt=f.attempt,
                )
                for f in findings
            ]

        if "commands" in want:
            cmds = _sdk_list_evidence_commands(run_id, cwd=None)
            out["commands"] = [
                EvidenceCommandSliceRecord(
                    argv_summary=c.argv_summary, cwd=c.cwd,
                    exit_code=c.exit_code, duration_s=c.duration_s,
                    outcome=c.outcome,
                )
                for c in cmds
            ]

        if "artifacts" in want:
            arts = _sdk_list_evidence_artifacts(run_id, cwd=None)
            out["artifacts"] = [
                EvidenceArtifactSliceRecord(
                    path=a.path, kind=a.kind, size_bytes=a.size_bytes,
                )
                for a in arts
            ]

        if "errors" in want:
            eh = _sdk_get_errors_halt(run_id, cwd=None)
            # Single source: the delivery/waiver projection is built from
            # the SAME rollup list as the raw ``errors`` field, never a
            # second meta read — so the two can never drift.
            errors_list = list(eh.errors)
            out["errors"] = ErrorsHaltSliceRecord(
                status=eh.status,
                errors=errors_list,
                halt_reason=eh.halt_reason,
                halted_at=eh.halted_at,
                error_summary=eh.error_summary,
                implement_delivery=_project_implement_delivery(errors_list),
                # Core-typed provider pressure from the SAME projection mapping
                # + shared helper as status / diagnose / summary — built from
                # the ``eh`` already fetched here (no second SDK read), so the
                # errors slice carries the identical typed condition (not a
                # presence flag) and never loses the core fact. ``None`` for a
                # generic failure.
                provider_pressure=build_provider_pressure(
                    project_provider_pressure_from_errors_halt(run_id, eh),
                ),
            )

        if "sub_runs" in want:
            links = _sdk_list_sub_runs(run_id, cwd=None)
            out["sub_runs"] = [
                SubRunLinkRecord(
                    name=link.name, status=link.status, run_dir=link.run_dir,
                )
                for link in links
            ]

        if "receipts" in want:
            receipts = _sdk_list_subtask_receipts(run_id, cwd=None)
            out["receipts"] = [
                SubtaskReceiptRecord(
                    subtask_id=r.subtask_id,
                    state=r.state,
                    runtime=r.runtime,
                    model=r.model,
                    skill=r.skill,
                    depends_on=list(r.depends_on),
                    done_criteria=list(r.done_criteria),
                    duration=r.duration,
                    error=r.error,
                    criteria_report=[
                        CriterionReportRecord(
                            index=c.index, criterion=c.criterion,
                            met=c.met, evidence=c.evidence,
                        )
                        for c in r.criteria_report
                    ],
                    attestation_summary=r.attestation_summary,
                    attestation_error=r.attestation_error,
                    attestation_repaired=r.attestation_repaired,
                )
                for r in receipts
            ]

        if "handoff_advice" in want:
            # Stage 0/1 handoff-advice evidence via the SDK projection — never a
            # direct pipeline import. ``None`` (no advisor surface, or a stale
            # core without the symbol) projects to a clean empty slice; the
            # projector never raises on absence. RunNotFound resolves through
            # map_sdk_errors.
            ev = (
                _sdk_list_handoff_advice(run_id, cwd=None)
                if _sdk_list_handoff_advice is not None
                else None
            )
            out["handoff_advice"] = _project_handoff_advice(ev)

        if "verification_receipts" in want:
            # Read the durable receipt artifacts under the run dir. The SDK
            # has no full-receipt reader (only the bundle summary), so this
            # is the T0-sanctioned artifact read-path. ``find_run_dir``
            # raises the typed MCP errors, which pass through map_sdk_errors
            # untouched.
            out["verification_receipts"] = _read_verification_receipts(
                find_run_dir(run_id),
            )

        if needs_timeline:
            # Availability is guaranteed by the precondition check above. The
            # official verification-gate timeline is a read-only durable SDK
            # projection (no pipeline import in MCP): the SDK owns the
            # classification + the six-value status enum, this layer is a pure
            # wire pass-through. ``cwd=None`` disables walk-up so the long-lived
            # server never binds to an arbitrary process cwd's runspace — the
            # same discipline as ``services.run_lookup`` and the other SDK
            # accessors. RunNotFound resolves through map_sdk_errors.
            #
            # ONE SDK read feeds BOTH slices: the timeline is a pure wire
            # pass-through and the cockpit a derived projection over the same
            # ``proj`` — never a second ``get_verification_timeline`` call.
            proj = _sdk_get_verification_timeline(run_id=run_id, cwd=None)
            if "verification_timeline" in want:
                out["verification_timeline"] = _project_verification_timeline(
                    proj,
                )
            if "verification_cockpit" in want:
                out["verification_cockpit"] = build_verification_cockpit(proj)

    return EvidenceResult(**out)


__all__ = ["inspect_run_evidence"]
