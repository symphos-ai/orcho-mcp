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

from orcho_mcp.errors import (
    InvalidPlanError,
    RunNotFoundError,
    WorkspaceNotResolvedError,
)
from orcho_mcp.schemas import (
    CorrectionSliceRecord,
    CriterionReportRecord,
    DeliverySummaryRecord,
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
    ReceiptEvidenceRecord,
    ScheduledGateEventRecord,
    ScheduledGateRowRecord,
    ScopeExpansionItemRecord,
    ScopeExpansionSliceRecord,
    SubRunLinkRecord,
    SubtaskReceiptRecord,
    VerificationCheckRecord,
    VerificationCommandRecord,
    VerificationReceiptRecord,
    VerificationTimelineRecord,
)
from orcho_mcp.services.delivery_gate import (
    _extract_commit_delivery,
    _extract_delivery_branch,
    _extract_delivery_notices,
    _extract_pr_url,
    _map_pr_intent,
    _map_release,
)
from orcho_mcp.services.errors import map_sdk_errors
from orcho_mcp.services.run_artifacts import (
    get_run_allowed_modifications as _get_run_allowed_modifications,
    get_run_meta_raw as _get_run_meta_raw,
)
from orcho_mcp.services.run_lookup import find_run_dir
from orcho_mcp.services.run_projection import (
    build_provider_pressure,
    project_provider_pressure_from_errors_halt,
)
from orcho_mcp.services.status_merge import (
    merged_halt_reason_from_meta,
    merged_status_from_meta,
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


_ADVISORY_FINDING_PHASE = "validate_plan"


def _as_int(value: Any) -> int:
    """Coerce a meta scalar to ``int``; non-ints (incl. bool) → ``0``.

    Mirrors orcho-core's ``pipeline.project.finalization._as_int`` so the
    whole-plan-delivered replication matches core's own numeric coercion.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def _implement_whole_plan_delivered(meta: dict[str, Any]) -> bool:
    """Thin sanctioned replication of core's advisory-findings gate.

    Mirrors ``pipeline.project.finalization._implement_whole_plan_delivered``
    (the same way ``services.run_artifacts._safe_decision_id`` replicates a
    core-internal helper that the SDK does not re-export): the implement record
    carries ``output``, has no ``guardrail_blocked`` / ``failed`` / ``error``,
    and ran no subtask DAG (its ``meta`` has no positive ``subtask_count``).
    True marks the small-task-style path where a bypassed validate_plan's
    critique was forwarded into a successful whole-plan implement, so those
    plan findings are ADVISORY (visible, not resolved, not active blockers)
    rather than active release risks. This is a durable-data rule only — no
    LLM classification.

    Defensive: any missing / malformed key yields ``False`` (findings stay
    active), never an exception.
    """
    phases = meta.get("phases") if isinstance(meta, dict) else None
    implement = phases.get("implement") if isinstance(phases, dict) else None
    if not isinstance(implement, dict):
        return False
    if not implement.get("output"):
        return False
    if any(implement.get(key) for key in ("guardrail_blocked", "failed", "error")):
        return False
    imeta = implement.get("meta")
    return not (isinstance(imeta, dict) and _as_int(imeta.get("subtask_count")) > 0)


def _phase_attempts(value: Any) -> list[dict[str, Any]]:
    """Normalise a ``meta.phases[name]`` slot into a list of attempt dicts.

    Mirrors core's ``pipeline.project.finalization._phase_attempts`` (and the
    SDK's own copy): an attempt-list phase stays a list, a single-mapping phase
    wraps to a one-element list, anything else is empty.
    """
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _validate_plan_attempt_approved(attempt: dict[str, Any]) -> bool:
    """Whether a ``validate_plan`` attempt was approved (core parity).

    Thin replication of core's ``_attempt_approved`` narrowed to the
    ``validate_plan`` phase (the ``review_changes`` / ``final_acceptance``
    branches never apply here): an ``APPROVED`` verdict (case/whitespace
    normalised, mirroring ``pipeline.run_state.release_verdict.is_approved``) or
    an explicit boolean ``approved`` field. Durable-data only — no LLM judgment.
    """
    verdict = attempt.get("verdict")
    if isinstance(verdict, str) and verdict.strip().upper() == "APPROVED":
        return True
    approved = attempt.get("approved")
    return approved if isinstance(approved, bool) else False


def _advisory_validate_plan_attempt(meta: dict[str, Any]) -> int | None:
    """The attempt number of the latest ``validate_plan`` attempt when advisory.

    Mirrors core's ``_review_finding_summary`` advisory rule exactly: findings
    are advisory ONLY for the LATEST ``validate_plan`` attempt, and ONLY when (a)
    the implement delivered the whole plan and (b) that latest attempt was NOT
    approved. Findings from earlier attempts are historical/resolved (neither
    active nor advisory); if the latest attempt is approved, core marks nothing
    advisory. SDK ``list_findings`` flattens findings across ALL attempts, so we
    must key advisory on the latest attempt number rather than the phase alone.

    Returns the latest attempt's number (the same value the SDK stamps on its
    findings: ``int(attempt['attempt'] or attempt_idx)``) so the caller can
    match findings by ``attempt``; ``None`` when no advisory findings apply.

    Defensive: any missing / malformed key yields ``None`` (findings stay
    active), never an exception.
    """
    if not _implement_whole_plan_delivered(meta):
        return None
    phases = meta.get("phases") if isinstance(meta, dict) else None
    attempts = _phase_attempts(
        phases.get(_ADVISORY_FINDING_PHASE) if isinstance(phases, dict) else None,
    )
    if not attempts:
        return None
    latest = attempts[-1]
    if _validate_plan_attempt_approved(latest):
        return None
    return _as_int(latest.get("attempt")) or len(attempts)


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
    data: dict[str, Any],
    artifact_path: Path,
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
        checks.append(
            VerificationCheckRecord(
                name=str(c.get("name", "")),
                expected=_optional_str(c.get("expected")),
                actual=_optional_str(c.get("actual")),
                passed=bool(c.get("passed")),
            )
        )

    raw_commands = data.get("commands")
    commands: list[VerificationCommandRecord] = []
    for cm in raw_commands if isinstance(raw_commands, list) else []:
        if not isinstance(cm, dict):
            continue
        commands.append(
            VerificationCommandRecord(
                argv=_coerce_argv(cm.get("argv")),
                exit_code=_coerce_exit_code(cm.get("exit_code")),
            )
        )

    all_passed = bool(checks) and all(c.passed for c in checks)
    raw_round = data.get("round")
    round_val = (
        raw_round
        if isinstance(raw_round, int)
        and not isinstance(
            raw_round,
            bool,
        )
        else None
    )

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
    """Map the canonical SDK ledger one-to-one onto the public wire record."""

    def receipt(value: Any) -> ReceiptEvidenceRecord | None:
        if value is None:
            return None
        return ReceiptEvidenceRecord(
            classification=value.classification,
            path=value.path,
            source=value.source,
            inherited=value.inherited,
            reason=value.reason,
            rerun=value.rerun,
        )

    return VerificationTimelineRecord(
        schema_version=proj.schema_version,
        run_id=str(proj.run_id),
        project=proj.project,
        finalized=proj.finalized,
        rows=[
            ScheduledGateRowRecord(
                command=row.command,
                hook=row.hook,
                phase=row.phase,
                declared=row.declared,
                selectable=row.selectable,
                selected=row.selected,
                execution_policy=row.execution_policy,
                consequence=row.consequence,
                disposition=row.disposition,
                selection_reason=row.selection_reason,
                executor=row.executor,
                trigger=row.trigger,
                receipt_evidence=receipt(row.receipt_evidence),
            )
            for row in proj.rows
        ],
        events=[
            ScheduledGateEventRecord(
                command=event.command,
                hook=event.hook,
                phase=event.phase,
                kind=event.kind,
                outcome=event.outcome,
                reason=event.reason,
                receipt_evidence=receipt(event.receipt_evidence),
            )
            for event in proj.events
        ],
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


_SCOPE_STATUS_PREFIX = "scope_expansion_"


def _normalize_scope_classification(raw: Any) -> str:
    """Normalise a core scope-expansion status onto the wire vocabulary.

    Core's ``ScopeExpansionStatus`` (``pipeline.engine.scope_expansion``) writes
    its durable ``status`` as the ENUM VALUE — ``scope_expansion_notice`` /
    ``scope_expansion_risk`` / ``scope_expansion_blocker`` — via
    ``ScopeExpansionItem.to_dict()``. The wire contract, schema, docs, and
    captain clients all branch on the bare ``notice`` / ``risk`` / ``blocker``
    tokens, so strip the ``scope_expansion_`` prefix here. A bare token (older
    core, or a hand-built fixture) passes through unchanged; an empty / missing
    status defaults to ``notice`` (the benign, informational classification).
    """
    s = str(raw) if raw is not None else ""
    if not s:
        return "notice"
    if s.startswith(_SCOPE_STATUS_PREFIX):
        return s[len(_SCOPE_STATUS_PREFIX) :] or "notice"
    return s


def _project_scope_expansion(run_id: str) -> ScopeExpansionSliceRecord:
    """Project the ADR 0110 scope-expansion audit from the run's meta.

    Reads ``meta['phases']['final_acceptance']['scope_expansion']`` (a dict
    carrying ``items`` and ``has_blocker``) via the sanctioned durable read
    ``services.run_artifacts.get_run_meta_raw`` — the same read-path discipline
    as ``verification_receipts`` (no direct file read here, no SDK re-implement).

    Defensive like ``_read_verification_receipts``: a missing / unreadable run,
    a non-dict meta, an absent / non-dict ``scope_expansion`` key, or non-list
    ``items`` all collapse to a clean empty slice (``items=[]``,
    ``has_blocker=False``) — never an exception. A stale core that never wrote
    the key therefore leaves ``slice='all'`` intact with an empty scope slice.

    Each item projects ``path`` (required — items without one are skipped),
    ``classification`` normalised from the core item's ``status`` onto the bare
    ``notice`` / ``risk`` / ``blocker`` wire vocabulary (core writes the enum
    VALUE ``scope_expansion_notice`` / ``…_risk`` / ``…_blocker`` — see
    ``_normalize_scope_classification``), ``category``, and ``evidence``.

    Product semantics: this is the ADR 0110 plan-vs-delivered scope axis, a
    SEPARATE fact from the delivery ``scope_disclosure`` (strict-mono sibling
    files behind a shipping block) surfaced on ``DeliveryGateProjection``. A
    ``notice`` is informational ONLY — the projection forms no operator handoff
    / next_action for it (this slice carries no ``next_actions`` field at all).
    A ``blocker`` is reflected as the decision condition via ``has_blocker``,
    without changing any core policy.
    """
    try:
        meta = _get_run_meta_raw(run_id)
    except (RunNotFoundError, WorkspaceNotResolvedError):
        return ScopeExpansionSliceRecord()
    if not isinstance(meta, dict):
        return ScopeExpansionSliceRecord()
    phases = meta.get("phases")
    final_acceptance = (
        phases.get("final_acceptance")
        if isinstance(
            phases,
            dict,
        )
        else None
    )
    raw = (
        final_acceptance.get("scope_expansion")
        if isinstance(
            final_acceptance,
            dict,
        )
        else None
    )
    if not isinstance(raw, dict):
        return ScopeExpansionSliceRecord()

    raw_items = raw.get("items")
    items: list[ScopeExpansionItemRecord] = []
    for it in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(it, dict):
            continue
        path = _optional_str(it.get("path"))
        if path is None:
            continue
        items.append(
            ScopeExpansionItemRecord(
                path=path,
                classification=_normalize_scope_classification(it.get("status")),
                category=_optional_str(it.get("category")),
                evidence=_str_list(it.get("evidence")),
            )
        )
    return ScopeExpansionSliceRecord(
        items=items,
        has_blocker=bool(raw.get("has_blocker")),
    )


# Explicit maps over the core ``CommitDeliveryStatus`` vocabulary
# (``pipeline.engine.commit_delivery.CommitDeliveryStatus``). Anything outside
# these sets is an unrecognized status → all four booleans False, raw status
# preserved.
_DELIVERY_APPLIED_STATUSES = frozenset({"applied_uncommitted", "committed"})
_DELIVERY_FAILED_STATUSES = frozenset(
    {
        "commit_failed",
        "apply_failed",
        "halted",
        "verification_blocked",
        "target_dirty",
    }
)


def _project_delivery(
    run_id: str,
    errors: list[dict[str, Any]],
) -> DeliverySummaryRecord | None:
    """Project the post-release commit-delivery outcome from durable meta.

    Reads ``meta['commit_delivery']`` via the shared
    ``services.delivery_gate._extract_commit_delivery`` (single source for the
    top-level / legacy-nested shape) and maps the core
    ``CommitDeliveryStatus`` vocabulary onto the explicit
    ``applied`` / ``committed`` / ``skipped`` / ``failed`` booleans. This is a
    read-only evidence projection — it never resolves available actions or
    mutates state (that is the interactive ``DeliveryGateProjection``).

    ``release_verdict`` reuses ``services.delivery_gate._map_release`` so an
    approved correction child re-run after a ``gate_rerun`` reads ``approved``
    from its OWN ``commit_delivery`` block. ``implement_delivery`` reuses
    ``_project_implement_delivery`` over the SAME errors-rollup the ``errors``
    slice surfaces — never a second meta read.

    Defensive: an unknown / unreadable run or a meta with no ``commit_delivery``
    block yields ``None`` (not an exception), so ``slice='all'`` stays whole.
    """
    try:
        meta = _get_run_meta_raw(run_id)
    except (RunNotFoundError, WorkspaceNotResolvedError):
        return None
    cd = _extract_commit_delivery(meta) if isinstance(meta, dict) else None
    if cd is None:
        return None

    status = _optional_str(cd.get("status"))
    commit_sha = _optional_str(cd.get("commit_sha"))
    applied = status in _DELIVERY_APPLIED_STATUSES
    committed = status == "committed" or commit_sha is not None
    skipped = status == "skipped"
    failed = status in _DELIVERY_FAILED_STATUSES

    halt_reason = _optional_str(cd.get("halt_reason"))
    if halt_reason is None and isinstance(meta, dict):
        halt_reason = _optional_str(meta.get("halt_reason"))

    return DeliverySummaryRecord(
        release_verdict=_map_release(cd),
        decision_status=status,
        action=_optional_str(cd.get("action")),
        applied=applied,
        committed=committed,
        commit_sha=commit_sha,
        skipped=skipped,
        failed=failed,
        halt_reason=halt_reason,
        # ADR 0119 delivery-branch facts, mapped through the SAME shared
        # ``services.delivery_gate`` helpers the gate projection uses (single
        # source, no second meta read). Absent → None / [], never fabricated.
        delivery_branch=_extract_delivery_branch(cd),
        pr_url=_extract_pr_url(cd),
        delivery_notices=_extract_delivery_notices(cd),
        pr_intent=_map_pr_intent(cd),
        # Single source: the same implement-verdict projection as the errors
        # slice, built from the already-fetched errors-rollup (no second meta
        # read), so the two records can never drift.
        implement_delivery=_project_implement_delivery(errors),
    )


def _project_correction(run_id: str) -> CorrectionSliceRecord | None:
    """Project the ADR 0098 correction fixed-point / non-convergence block.

    Reads ``meta['correction_fixed_point']`` (the durable non-convergence block
    core writes when a correction child repeats its parent's blockers) plus the
    run's ``halt_reason``. ``non_converging`` True is an OPERATOR-DECISION
    condition — ``suggested_actions`` are advisory next-step hints for the
    captain, NEVER an auto-applied fix.

    Defensive: an unknown / unreadable run or a meta with no
    ``correction_fixed_point`` block yields ``None`` (not an exception).
    """
    try:
        meta = _get_run_meta_raw(run_id)
    except (RunNotFoundError, WorkspaceNotResolvedError):
        return None
    block = meta.get("correction_fixed_point") if isinstance(meta, dict) else None
    if not isinstance(block, dict):
        return None

    repeated = _str_list(block.get("repeated"))
    reason = _optional_str(block.get("reason"))
    halt_reason = _optional_str(meta.get("halt_reason"))
    # The block is only ever written on non-convergence; derive the flag from
    # the durable signals rather than hardcoding it.
    non_converging = (
        halt_reason == "correction_not_converging" or bool(repeated) or reason is not None
    )
    return CorrectionSliceRecord(
        non_converging=non_converging,
        repeated=repeated,
        parent_run_id=_optional_str(block.get("parent_run_id")),
        child_run_id=_optional_str(block.get("child_run_id")),
        suggested_actions=_str_list(block.get("suggested_actions")),
        reason=reason,
    )


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
        "all",
        "plan",
        "findings",
        "commands",
        "artifacts",
        "errors",
        "sub_runs",
        "receipts",
        "verification_receipts",
        "verification_timeline",
        "verification_cockpit",
        "handoff_advice",
        "scope_expansion",
        "delivery",
        "correction",
    }
    if slice not in valid_slices:
        raise InvalidPlanError(
            f"orcho_run_evidence: slice must be one of {sorted(valid_slices)!r}, got {slice!r}"
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
    try:
        run_dir = find_run_dir(run_id)
    except (RunNotFoundError, WorkspaceNotResolvedError):
        # Keep the SDK-error seam usable for narrow evidence projections; a
        # real resolved run still receives the settled-supervisor overlay.
        run_dir = None
    try:
        evidence_meta = _get_run_meta_raw(run_id)
    except (RunNotFoundError, WorkspaceNotResolvedError):
        evidence_meta = {}

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
                # The SDK plan summary carries no ``allowed_modifications``; the
                # single source is the durable ``parsed_plan.json`` top-level
                # field, read defensively (absent → empty).
                allowed_modifications=_get_run_allowed_modifications(run_id),
            )

        if "findings" in want:
            findings = _sdk_list_findings(
                run_id,
                cwd=None,
                phases=phases_kw,
                **sev_kw,
            )
            # Advisory rule (AC5): a ``validate_plan`` finding forwarded into a
            # successful whole-plan implement is advisory (visible, not active),
            # replicating core's ``_review_finding_summary`` gate over durable
            # meta. Core marks advisory ONLY the LATEST validate_plan attempt's
            # findings, and ONLY when that attempt was not approved; earlier
            # attempts are historical/resolved. SDK ``list_findings`` flattens
            # every attempt, so match on the latest attempt number rather than
            # the phase alone (fixes over-marking on multi-attempt runs).
            # Defensive: an unreadable run leaves every finding active. Read meta
            # once for the whole batch.
            try:
                _meta = _get_run_meta_raw(run_id)
            except (RunNotFoundError, WorkspaceNotResolvedError):
                _meta = {}
            _advisory_attempt = _advisory_validate_plan_attempt(_meta)
            out["findings"] = [
                FindingRecord(
                    id=f.id,
                    severity=f.severity,
                    title=f.title,
                    body=f.body,
                    required_fix=f.required_fix,
                    file=f.file,
                    line=f.line,
                    phase=f.phase,
                    attempt=f.attempt,
                    advisory=(
                        _advisory_attempt is not None
                        and f.phase == _ADVISORY_FINDING_PHASE
                        and f.attempt == _advisory_attempt
                    ),
                )
                for f in findings
            ]

        if "commands" in want:
            cmds = _sdk_list_evidence_commands(run_id, cwd=None)
            out["commands"] = [
                EvidenceCommandSliceRecord(
                    argv_summary=c.argv_summary,
                    cwd=c.cwd,
                    exit_code=c.exit_code,
                    duration_s=c.duration_s,
                    outcome=c.outcome,
                    source=c.source,
                    identity_digest=c.identity_digest,
                    phase=c.phase,
                    state=c.state,
                    executable=c.executable,
                    started_at=c.started_at,
                    finished_at=c.finished_at,
                    artifact_path=c.artifact_path,
                    degraded_reason=c.degraded_reason,
                )
                for c in cmds
            ]

        if "artifacts" in want:
            arts = _sdk_list_evidence_artifacts(run_id, cwd=None)
            out["artifacts"] = [
                EvidenceArtifactSliceRecord(
                    path=a.path,
                    kind=a.kind,
                    size_bytes=a.size_bytes,
                )
                for a in arts
            ]

        # The delivery/waiver implement projection reuses the SAME errors-rollup
        # the ``errors`` slice surfaces (single source, never a second meta
        # read). Fetch it once when either the ``errors`` or ``delivery`` slice
        # needs it, then share it with both.
        errors_list: list[dict[str, Any]] = []
        if want & {"errors", "delivery"}:
            eh_kwargs = {"cwd": None}
            if run_dir is not None:
                eh_kwargs["runs_dir"] = run_dir.parent
            eh = _sdk_get_errors_halt(run_id, **eh_kwargs)
            errors_list = list(eh.errors)

        if "errors" in want:
            out["errors"] = ErrorsHaltSliceRecord(
                status=(merged_status_from_meta(evidence_meta, run_dir) if run_dir else None) or eh.status,
                errors=errors_list,
                halt_reason=(merged_halt_reason_from_meta(evidence_meta, run_dir) if run_dir else None) or eh.halt_reason,
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
                    name=link.name,
                    status=link.status,
                    run_dir=link.run_dir,
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
                            index=c.index,
                            criterion=c.criterion,
                            met=c.met,
                            evidence=c.evidence,
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

        if "scope_expansion" in want:
            # ADR 0110 scope-expansion audit projected from durable meta via
            # ``services.run_artifacts.get_run_meta_raw`` (the sanctioned
            # read-path — no direct file read, no core re-implement). Fully
            # defensive: a missing / malformed audit yields a clean empty slice,
            # never an exception, so a stale core keeps ``slice='all'`` whole.
            # This axis is distinct from the delivery ``scope_disclosure`` on
            # ``DeliveryGateProjection`` (strict-mono sibling shipping block).
            out["scope_expansion"] = _project_scope_expansion(run_id)

        if "delivery" in want:
            # Read-only post-release commit-delivery outcome projected from
            # durable meta. ``implement_delivery`` reuses the SAME errors-rollup
            # fetched above (single source, no second meta read). ``None`` when
            # the run recorded no commit-delivery decision.
            out["delivery"] = _project_delivery(run_id, errors_list)

        if "correction" in want:
            # ADR 0098 correction fixed-point / non-convergence, projected from
            # durable meta. ``non_converging`` is an operator-decision condition;
            # ``suggested_actions`` are advisory, never auto-applied. ``None``
            # when core recorded no fixed-point block.
            out["correction"] = _project_correction(run_id)

        if needs_timeline:
            # Availability is guaranteed by the precondition check above. The
            # official verification-gate timeline is a read-only durable SDK
            # projection (no pipeline import in MCP): this layer forwards its
            # canonical ledger rows and identity-scoped events unchanged.
            # ``cwd=None`` disables walk-up so the long-lived
            # server never binds to an arbitrary process cwd's runspace — the
            # same discipline as ``services.run_lookup`` and the other SDK
            # accessors. RunNotFound resolves through map_sdk_errors.
            #
            # ONE SDK read feeds BOTH public names. They are intentionally the
            # same canonical ledger projection, not separate semantic views.
            proj = _sdk_get_verification_timeline(run_id=run_id, cwd=None)
            timeline = _project_verification_timeline(proj)
            if "verification_timeline" in want:
                out["verification_timeline"] = timeline
            if "verification_cockpit" in want:
                out["verification_cockpit"] = timeline

    return EvidenceResult(**out)


__all__ = ["inspect_run_evidence"]
