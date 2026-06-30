"""orcho_mcp.services.run_lineage — durable recovery-lineage projection.

This module is the MCP-side *thin projection* of one core read-model: *what is
the safe continuation subject of this run?* The classification itself —
terminality, lineage, worktree-continuity, plan-artifact attribution, and the
closed continuation vocabulary — is owned by core's
:func:`sdk.run_control.recovery_lineage`. This module no longer re-derives any
of it. Its only jobs are:

1. **Seed core with supervisor-merged facts.** The pipeline owns ``meta.json``
   but supervisor cancellation paths bypass its writer, so the inspected run's
   (and each source candidate's) on-disk ``status`` / ``halt_reason`` can be
   stale. We reconcile them via :func:`orcho_mcp.services.status_merge.merged_meta`
   and hand the result to core as ``meta=`` / ``source_meta=``. Without this a
   stale ``status='running'`` source could make core recommend a blind
   ``recover_via_source_run`` for a source the supervisor already settled.
2. **Map the typed core read-model onto the wire dataclass.** ``RecoveryLineage``
   → :class:`RecoveryLineageProjection` field-for-field (the lone shape change is
   ``missing_facts``: ``tuple`` in core → ``list`` on the wire).

The closed continuation vocabulary (:class:`ContinuationSubject` /
:class:`RecommendedNextAction` and the :data:`CONTINUATION_SUBJECTS` /
:data:`RECOMMENDED_NEXT_ACTIONS` frozensets) and the
:class:`RecoveryLineageProjection` shape stay MCP-side: they are the wire
dictionary the schema layer projects into Literal enums, kept in lockstep with
core's string constants (core owns resolution; MCP owns the wire word).

Defensive contract (unchanged): every read is wrapped so any failure degrades
to a typed ``unknown`` / ``stop_unknown`` continuation rather than raising. When
the inspected run's meta cannot be read we defer to core's canonical
unreadable-meta dead-end (it owns the ``missing_facts`` vocabulary), so the
standalone and diagnosis-attached recovery read-models can never diverge.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sdk import load_meta as _sdk_load_meta
from sdk.run_control import RecoveryLineage, recovery_lineage

from orcho_mcp.services.run_lookup import find_run_dir
from orcho_mcp.services.run_projection import project_followup_lineage
from orcho_mcp.services.status_merge import merged_meta

# ── Typed continuation vocabulary ───────────────────────────────────────────
#
# The two string-constant families the wire layer (T3) projects into Literal
# enums. Kept as plain classes-of-constants so both the projection and the
# schema reference one source of truth without importing pydantic here. The
# values mirror core's ``sdk.run_control.recovery_lineage`` constants verbatim —
# core owns resolution, this module owns the wire word; keep them in lockstep.


class ContinuationSubject:
    """The durable subject a run's continuation should act on."""

    SOURCE_RUN_CHECKPOINT = "source_run_checkpoint"
    ACTIVE_CHILD_RUN = "active_child_run"
    DELIVERY_GATE = "delivery_gate"
    PLAN_ARTIFACT = "plan_artifact"
    NONE = "none"
    UNKNOWN = "unknown"


class RecommendedNextAction:
    """The typed next action a captain should take for the continuation."""

    RESUME_SOURCE_RUN = "resume_source_run"
    RESUME_ACTIVE_CHILD = "resume_active_child"
    DELIVERY_DECISION = "delivery_decision"
    START_FOLLOWUP = "start_followup"
    PLAN_ARTIFACT_CONTINUATION = "plan_artifact_continuation"
    STOP_UNKNOWN = "stop_unknown"


CONTINUATION_SUBJECTS = frozenset({
    ContinuationSubject.SOURCE_RUN_CHECKPOINT,
    ContinuationSubject.ACTIVE_CHILD_RUN,
    ContinuationSubject.DELIVERY_GATE,
    ContinuationSubject.PLAN_ARTIFACT,
    ContinuationSubject.NONE,
    ContinuationSubject.UNKNOWN,
})
RECOMMENDED_NEXT_ACTIONS = frozenset({
    RecommendedNextAction.RESUME_SOURCE_RUN,
    RecommendedNextAction.RESUME_ACTIVE_CHILD,
    RecommendedNextAction.DELIVERY_DECISION,
    RecommendedNextAction.START_FOLLOWUP,
    RecommendedNextAction.PLAN_ARTIFACT_CONTINUATION,
    RecommendedNextAction.STOP_UNKNOWN,
})


# The full durable-fact set a total dead-end lacks, mirrored from core's
# ``_ALL_MISSING_FACTS``. Used only by the never-raise safety net below: when a
# core call or the field mapper unexpectedly raises we cannot probe which facts
# are present, so we report the same complete ``missing_facts`` as core's
# canonical unreadable-meta dead-end. This is not a classifier — resolution
# stays core-owned; it is the defensive contract's fallback vocabulary only.
_UNRESOLVED_MISSING_FACTS = (
    "no source/parent run id",
    "no plan artifact",
    "no delivery gate",
    "no active child",
)


def _optional_str(value: object) -> str | None:
    """Coerce a value to a non-empty ``str``, else ``None``."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s or None


@dataclass(frozen=True)
class RecoveryLineageProjection:
    """Typed durable recovery-lineage classification of a single run.

    ``continuation_subject`` is one of :data:`CONTINUATION_SUBJECTS` and
    ``recommended_next_action`` one of :data:`RECOMMENDED_NEXT_ACTIONS`
    (``None`` only for the non-terminal "resume the run itself" case).
    ``recommended_run_id`` is the source / child / plan-owning run a captain
    should act on. ``missing_facts`` is non-empty only for the ``unknown``
    dead-end and names which durable facts are absent. ``reason`` is one line
    assembled from persisted facts — never parsed from log prose.

    The MCP-side mirror of core's :class:`sdk.run_control.RecoveryLineage`: every
    field is a 1:1 projection of it (see :func:`_project_recovery_lineage`); the
    lone shape difference is ``missing_facts`` — ``list[str]`` here vs
    ``tuple[str, ...]`` in core (same values, same order).
    """

    run_id: str
    is_terminal_or_rejected: bool
    continuation_subject: str
    recommended_next_action: str | None
    recommended_run_id: str | None = None
    source_run_id: str | None = None
    source_status: str | None = None
    source_resumable: bool = False
    source_worktree_preserved: bool = False
    plan_subject_available: bool = False
    active_child_run_id: str | None = None
    missing_facts: list[str] = field(default_factory=list)
    reason: str = ""


def _safe_followup_lineage(run_id: str):
    """Project the follow-up lineage, swallowing read errors to ``None``."""
    try:
        return project_followup_lineage(run_id)
    except Exception:
        return None


def _safe_candidate_merged_meta(candidate_run_id: str) -> dict | None:
    """Load + supervisor-merge one source candidate's meta, or ``None``.

    The provider seam core's ``recovery_lineage`` reads for the source
    candidates: feeding the supervisor-merged ``status`` keeps a stale on-disk
    ``status='running'`` source from forcing a blind ``recover_via_source_run``
    for a source the supervisor already settled as terminal. Any read failure
    degrades to ``None`` (the candidate is simply omitted, never an exception) so
    a corrupt source-meta cannot break the inspected run's lineage.
    """
    try:
        run_dir = find_run_dir(candidate_run_id)
        meta = _sdk_load_meta(run_dir) or {}
    except Exception:
        return None
    if not isinstance(meta, dict):
        return None
    return merged_meta(meta, run_dir)


def _build_source_meta(run_id: str, meta: dict) -> dict[str, dict]:
    """Build the ``{candidate_run_id: supervisor-merged meta}`` seam for core.

    Candidates are the durable source pointers core's resolver consults — the
    inspected run's ``parent_run_id`` (via the follow-up lineage projection) then
    its ``plan_source_run_id`` — each loaded once and supervisor-merged. A
    duplicate or unreadable candidate is skipped; the map is empty when no source
    pointer resolves (core then reports no source).
    """
    lineage = _safe_followup_lineage(run_id)
    parent_run_id = lineage.parent_run_id if lineage else None
    plan_source_run_id = _optional_str(meta.get("plan_source_run_id"))

    source_meta: dict[str, dict] = {}
    for candidate in (parent_run_id, plan_source_run_id):
        if not candidate or candidate in source_meta:
            continue
        merged = _safe_candidate_merged_meta(candidate)
        if merged is not None:
            source_meta[candidate] = merged
    return source_meta


def _resolve_recovery_inputs(
    run_id: str,
) -> tuple[dict | None, dict[str, dict] | None]:
    """Resolve the supervisor-merged ``meta`` / ``source_meta`` core inputs.

    Returns ``(resolved_meta, source_meta)`` for the inspected run. When the
    inspected run's meta cannot be read at all (missing run / read error), both
    are ``None`` so :func:`recovery_lineage` is left to produce its canonical
    unreadable-meta ``unknown`` / ``stop_unknown`` dead-end (core owns the
    ``missing_facts`` vocabulary) rather than this module re-deriving it.
    """
    try:
        run_dir = find_run_dir(run_id)
        meta = _sdk_load_meta(run_dir) or {}
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        return None, None
    return merged_meta(meta, run_dir), _build_source_meta(run_id, meta)


def _project_recovery_lineage(
    lineage: RecoveryLineage,
) -> RecoveryLineageProjection:
    """Map core's :class:`RecoveryLineage` onto the wire projection field-for-field.

    The only shape change is ``missing_facts`` (``tuple`` → ``list``); every other
    field is carried verbatim so the wire form stays byte-identical to the
    pre-migration projection.
    """
    return RecoveryLineageProjection(
        run_id=lineage.run_id,
        is_terminal_or_rejected=lineage.is_terminal_or_rejected,
        continuation_subject=lineage.continuation_subject,
        recommended_next_action=lineage.recommended_next_action,
        recommended_run_id=lineage.recommended_run_id,
        source_run_id=lineage.source_run_id,
        source_status=lineage.source_status,
        source_resumable=lineage.source_resumable,
        source_worktree_preserved=lineage.source_worktree_preserved,
        plan_subject_available=lineage.plan_subject_available,
        active_child_run_id=lineage.active_child_run_id,
        missing_facts=list(lineage.missing_facts),
        reason=lineage.reason,
    )


def project_recovery_lineage(run_id: str) -> RecoveryLineageProjection:
    """Classify a run's durable recovery lineage into one typed projection.

    The single public resolver reused by ``orcho_run_diagnose`` and
    ``orcho_run_status``. A thin projection of core's
    :func:`sdk.run_control.recovery_lineage`: it reconciles the inspected run's
    and each source candidate's status/halt_reason with the supervisor state
    (:func:`_resolve_recovery_inputs`), feeds those merged facts to core as
    ``meta=`` / ``source_meta=``, then maps the typed :class:`RecoveryLineage`
    onto :class:`RecoveryLineageProjection` field-for-field.

    Fully defensive: ``recovery_lineage`` never raises for a non-empty run id —
    any read failure (including an unreadable inspected-run meta) degrades inside
    core to an ``unknown`` projection with a fact-built reason. The core call and
    the field mapper are additionally wrapped here so an empty/invalid run id
    (core ``ValueError``) or any unexpected core/mapper failure still degrades to
    the typed ``unknown`` / ``stop_unknown`` dead-end rather than propagating
    into ``orcho_run_status`` / ``orcho_run_diagnose``. The branch priority and
    continuation vocabulary are core-owned; see the module docstring.
    """
    resolved_meta, source_meta = _resolve_recovery_inputs(run_id)
    try:
        # ``cwd=None`` disables core's cwd walk-up so it resolves the runs dir
        # from ``$ORCHO_WORKSPACE`` exactly as MCP's ``find_run_dir`` does (the
        # long-lived server must never silently bind to whatever cwd it was
        # launched from — see ``services.run_lookup``). Source candidates resolve
        # through the same seam.
        lineage = recovery_lineage(
            run_id, cwd=None, meta=resolved_meta, source_meta=source_meta,
        )
        return _project_recovery_lineage(lineage)
    except Exception as exc:  # noqa: BLE001 - defensive: never raise to callers
        return RecoveryLineageProjection(
            run_id=run_id,
            is_terminal_or_rejected=False,
            continuation_subject=ContinuationSubject.UNKNOWN,
            recommended_next_action=RecommendedNextAction.STOP_UNKNOWN,
            missing_facts=list(_UNRESOLVED_MISSING_FACTS),
            reason=f"could not classify recovery lineage: {type(exc).__name__}",
        )


__all__ = [
    "CONTINUATION_SUBJECTS",
    "RECOMMENDED_NEXT_ACTIONS",
    "ContinuationSubject",
    "RecommendedNextAction",
    "RecoveryLineageProjection",
    "project_recovery_lineage",
]
