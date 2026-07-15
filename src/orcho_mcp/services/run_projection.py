"""orcho_mcp.services.run_projection — the single read-model projection owner.

This module owns the *projection* surface MCP read paths share: turning
on-disk run state (``meta.json`` + supervisor state) into normalised
read-models that presentation layers (``observe/``) render without
re-parsing the raw payloads.

Two projection families live here:

- **Pause / handoff read-model** (:func:`project_handoff_read_model`):
  parses ``meta.phase_handoff`` into a normalised
  :class:`HandoffReadModel` — available actions, handoff id, phase,
  trigger, incomplete-subtask count, and the *resolved* raw findings
  source (``meta.phase_handoff.findings`` when it is a non-empty list,
  else the SDK ``list_findings`` fallback, else ``[]``). Bounded
  truncation and prompt / choice / client-hint rendering stay in
  ``observe/handoff_hints.py`` on top of this read-model. ``services``
  must NOT import ``observe`` (cycle: ``observe.handoff_hints`` imports
  ``observe.summary._truncate``), so all presentation-side trimming is
  deliberately left out of this module.

- **Status / halt-reason projection**: ``merged_status_from_meta`` and
  ``merged_halt_reason_from_meta`` reconcile ``meta.status`` /
  ``meta.halt_reason`` with the supervisor state file. The
  implementation lives in :mod:`orcho_mcp.services.status_merge` (a pure
  module with no SDK imports — see its own docstring) and is re-exported
  here so the projection surface has one import home. This is the place
  Stage 7C extends when it grows the projected read-model.

Defensive contract: a corrupt ``meta`` / ``findings`` payload must never
raise — the normalisers coerce junk to safe defaults and the findings
fallback swallows SDK errors, so ``build_handoff_hint`` always returns a
valid hint at the exact moment the run paused. SDK errors from the
``load_meta`` read are translated through the shared
:func:`orcho_mcp.services.errors.map_sdk_errors` owner.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from sdk import (
    get_errors_halt as _sdk_get_errors_halt,
    list_findings as _sdk_list_findings,
    load_meta as _sdk_load_meta,
    load_phase_handoff_decision as _sdk_load_phase_handoff_decision,
    load_phase_handoff_decisions as _sdk_load_phase_handoff_decisions,
)
from sdk.run_control import run_diagnosis as _sdk_run_diagnosis

from orcho_mcp.schemas.shared import NextActionRecord, ProviderPressure
from orcho_mcp.services.delivery_gate import project_delivery_gate
from orcho_mcp.services.errors import map_sdk_errors
from orcho_mcp.services.run_control_boundary import (
    RunControlProjection,
    project_run_control,
)
from orcho_mcp.services.run_lookup import find_run_dir, runs_dir_or_raise
from orcho_mcp.services.status_merge import (
    merged_halt_reason_from_meta,
    merged_meta,
    merged_status_from_meta,
)

if TYPE_CHECKING:
    from pathlib import Path

    # Imported lazily inside ``project_run_diagnosis`` at runtime to avoid a
    # module-load cycle: ``services.run_lineage`` imports this module for the
    # terminality + lineage predicates it composes.
    from orcho_mcp.services.run_lineage import RecoveryLineageProjection


@dataclass(frozen=True)
class HandoffReadModel:
    """Normalised ``meta.phase_handoff`` read-model — presentation-free.

    Every field is already coerced to a safe shape, so observe-side
    rendering never has to defend against malformed meta:

    - ``actions``: cleaned ``available_actions`` list (order preserved).
    - ``handoff_id``: ``meta.phase_handoff.id`` as a non-empty str or None.
    - ``phase``: resolved phase (payload phase, else the caller's
      ``current_phase`` fallback) as a non-empty str or None.
    - ``trigger``: normalised trigger str or None.
    - ``incomplete_count``: |incomplete_subtasks ∪ missing_subtask_receipts|.
    - ``raw_findings``: the resolved findings *source* (untrimmed dicts /
      SDK objects); observe applies the bounded compaction.
    - ``verdict``: the runtime machine verdict label (``"REJECTED"`` /
      ``"APPROVED"``) carried verbatim from the payload, or None.
    - ``round_n`` / ``loop_max_rounds``: the auto-loop round counters
      persisted on the payload. ``round_n > loop_max_rounds`` is the
      structural marker of a human-directed retry round.
    - ``last_output``: the reviewer's last critique/output text
      (untrimmed); presentation layers truncate it to a preview.
    - ``decision_artifact_exists``: whether a persisted handoff decision
      already exists for ``handoff_id`` (read via
      ``sdk.load_phase_handoff_decision``; errors coerce to False).
    """

    actions: list[str] = field(default_factory=list)
    handoff_id: str | None = None
    phase: str | None = None
    trigger: str | None = None
    incomplete_count: int = 0
    raw_findings: list[object] = field(default_factory=list)
    verdict: str | None = None
    round_n: int | None = None
    loop_max_rounds: int | None = None
    last_output: str | None = None
    decision_artifact_exists: bool = False


def _normalise_handoff_actions(raw: object) -> list[str]:
    """Coerce ``meta.phase_handoff.available_actions`` to a clean list[str].

    Anything not a list returns ``[]``. List items are str-coerced and
    empty strings dropped — but order is preserved so the runtime's
    preference order survives into ``default_action``.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        s = str(item) if item is not None else ""
        if s:
            out.append(s)
    return out


def _normalise_handoff_trigger(raw: object) -> str | None:
    """Coerce ``meta.phase_handoff.trigger`` to a clean str or ``None``.

    Anything that is not a non-empty string returns ``None`` so callers
    treat a missing/malformed trigger as "no special phrasing" — the
    defensive-build invariant: the prompt must still render at the exact
    moment the run paused.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    return s or None


def _incomplete_subtask_count(artifacts: object) -> int:
    """Distinct incomplete-subtask count from the pause artifacts.

    ``N = len(incomplete_subtasks ∪ missing_subtask_receipts)`` — the
    union (by str identity) of the two ``meta.phase_handoff.artifacts``
    lists. Non-dict ``artifacts`` or non-list members contribute nothing;
    a malformed payload yields ``0``, never an exception.
    """
    if not isinstance(artifacts, dict):
        return 0
    ids: set[str] = set()
    for key in ("incomplete_subtasks", "missing_subtask_receipts"):
        raw = artifacts.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if item is not None:
                ids.add(str(item))
    return len(ids)


def _resolve_raw_findings(
    run_id: str, phase: str | None, handoff_payload: dict,
) -> list[object]:
    """Resolve the findings *source* for the handoff read-model.

    Source precedence:
      1. ``meta.phase_handoff.findings`` when it is a **non-empty** list —
         preserves the runtime's curated set;
      2. ``list_findings(run_id, phases=(phase,))`` as a defensive
         fallback when meta does not carry findings, including the
         ``findings: []`` shape (empty list = "not embedded here",
         evidence may still have something);
      3. ``[]`` on any failure or empty result — the handoff read-model
         must never block on the evidence path.

    Returns the untrimmed source items; observe owns the bounded
    compaction (coerce + per-field caps + 5-item limit).
    """
    raw = handoff_payload.get("findings")
    if isinstance(raw, list) and raw:
        return list(raw)
    try:
        phases_kw = (phase,) if phase else None
        sdk_results = _sdk_list_findings(run_id, cwd=None, phases=phases_kw)
        return list(sdk_results) if sdk_results else []
    except Exception:
        return []


def _coerce_optional_str(raw: object) -> str | None:
    """Coerce a payload value to a non-empty str, else ``None``.

    Unlike :func:`_normalise_handoff_trigger` this does **not** strip —
    ``last_output`` may carry meaningful leading/trailing whitespace and
    the presentation layer owns truncation; we only drop the empty/non-str
    case so callers see ``None`` rather than ``""``.
    """
    if not isinstance(raw, str):
        return None
    return raw or None


def _coerce_optional_int(raw: object) -> int | None:
    """Coerce a payload value to an ``int``, rejecting bool and non-ints.

    ``bool`` is a subclass of ``int`` in Python; a stray ``True`` round
    counter would otherwise read as ``1`` and silently corrupt the
    round-label classification, so it is rejected explicitly.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    return None


def _decision_artifact_exists(run_id: str, handoff_id: str | None) -> bool:
    """Whether a persisted decision artifact already exists for ``handoff_id``.

    Routed through ``sdk.load_phase_handoff_decision`` — the sanctioned
    reader. Any failure (no handoff id, missing run, or a corrupt artifact
    that the strict reader rejects) coerces to ``False`` so the read-model
    never raises at the exact moment a paused run is being inspected. A
    corrupt artifact reading as "absent" is safe: the resume path then
    suggests a fresh decision, and the decide call surfaces the corruption
    as a structured error rather than an opaque traceback here.
    """
    if not handoff_id:
        return False
    try:
        return _sdk_load_phase_handoff_decision(
            run_id, handoff_id, cwd=None,
        ) is not None
    except Exception:
        return False


def project_handoff_read_model(
    run_id: str, *, current_phase: str | None = None,
) -> HandoffReadModel:
    """Load + normalise the ``meta.phase_handoff`` read-model for a run.

    ``current_phase`` is the presentation-side fallback (typically the
    live ``RunEventsSummary.current_phase``) used when the pause payload
    omits its own ``phase`` — the resolved phase flows into both the
    read-model ``phase`` field and the findings fallback so they stay
    consistent with the pre-refactor behaviour.

    Defensive: corrupt meta / findings never raise. The ``load_meta``
    read is wrapped in the shared SDK→MCP error owner; the findings
    fallback swallows its own errors.
    """
    run_dir = find_run_dir(run_id)
    with map_sdk_errors(run_id):
        meta = _sdk_load_meta(run_dir) or {}
    handoff_payload = meta.get("phase_handoff") or {}
    if not isinstance(handoff_payload, dict):
        handoff_payload = {}

    actions = _normalise_handoff_actions(
        handoff_payload.get("available_actions"),
    )
    raw_id = handoff_payload.get("id")
    handoff_id = str(raw_id) if raw_id else None
    raw_phase = handoff_payload.get("phase")
    payload_phase = str(raw_phase) if raw_phase else None
    phase = payload_phase or current_phase

    trigger = _normalise_handoff_trigger(handoff_payload.get("trigger"))
    incomplete_count = _incomplete_subtask_count(
        handoff_payload.get("artifacts"),
    )
    raw_findings = _resolve_raw_findings(run_id, phase, handoff_payload)

    verdict = _coerce_optional_str(handoff_payload.get("verdict"))
    round_n = _coerce_optional_int(handoff_payload.get("round"))
    loop_max_rounds = _coerce_optional_int(handoff_payload.get("loop_max_rounds"))
    last_output = _coerce_optional_str(handoff_payload.get("last_output"))
    decision_artifact_exists = _decision_artifact_exists(run_id, handoff_id)

    return HandoffReadModel(
        actions=actions,
        handoff_id=handoff_id,
        phase=phase,
        trigger=trigger,
        incomplete_count=incomplete_count,
        raw_findings=raw_findings,
        verdict=verdict,
        round_n=round_n,
        loop_max_rounds=loop_max_rounds,
        last_output=last_output,
        decision_artifact_exists=decision_artifact_exists,
    )


# Single-project pause status that carries a ``meta.phase_handoff``
# decision payload. Kept local to the projection owner — observe and
# run_control consume the projection, never the status constant + key.
_PENDING_HANDOFF_STATUS = "awaiting_phase_handoff"


@dataclass(frozen=True)
class PendingHandoffProjection:
    """Operator-facing pending-handoff state, projected once for reuse.

    ``status``/``is_pending_handoff`` let callers branch without
    re-reading meta; the remaining fields mirror the
    :class:`HandoffReadModel` operator surface plus two derived fields:

    - ``round_label``: a coherent ``"<phase> automatic round R/M"`` or
      ``"<phase> human retry K"`` label (structural classification from
      ``round_n``/``loop_max_rounds`` — never an impossible ``R/M`` with
      ``R > M``). Deeper human-retry classification is a later slice.
    - ``suggested_next_action``: a one-line pointer at the right next
      tool (decide-then-resume when no decision artifact exists yet,
      resume when one already does).

    Consumed by ``observe.summary`` (status visibility without an
    ``orcho_run_watch`` trigger) and ``run_control.lifecycle`` (the
    structured pending-decision resume response).
    """

    status: str | None
    is_pending_handoff: bool
    handoff_id: str | None = None
    phase: str | None = None
    trigger: str | None = None
    verdict: str | None = None
    round_label: str | None = None
    available_actions: list[str] = field(default_factory=list)
    last_output: str | None = None
    decision_artifact_exists: bool = False
    suggested_next_action: str | None = None


def _round_label(
    phase: str | None, round_n: int | None, loop_max_rounds: int | None,
) -> str | None:
    """Render a coherent operator round label from the persisted counters.

    Structural classification only (no transient state read): a round is
    human-directed when ``round_n > loop_max_rounds`` — those one-shot
    retry rounds sit on top of the auto budget, so rendering them as
    ``R/M`` would print an impossible fraction. Returns ``None`` when the
    counters are absent.
    """
    if round_n is None or loop_max_rounds is None or loop_max_rounds < 1:
        return None
    label_phase = phase or "phase"
    if round_n > loop_max_rounds:
        retry_k = max(1, round_n - loop_max_rounds)
        return f"{label_phase} human retry {retry_k}"
    shown = round_n if round_n >= 1 else 1
    return f"{label_phase} automatic round {shown}/{loop_max_rounds}"


def _suggested_next_action(
    decision_artifact_exists: bool, actions: list[str],
) -> str:
    """One-line next-step pointer for a pending handoff.

    When a decision artifact already exists the operator's choice is
    recorded and only ``orcho_run_resume`` remains; otherwise the run is
    still awaiting a decision via ``orcho_phase_handoff_decide``.
    """
    if decision_artifact_exists:
        return (
            "call orcho_run_resume to apply the recorded decision and "
            "continue the run"
        )
    return (
        "call orcho_phase_handoff_decide to resolve the pause, then "
        "orcho_run_resume to continue"
    )


def project_pending_handoff(
    run_id: str, *, current_phase: str | None = None,
) -> PendingHandoffProjection:
    """Project the active single-project phase-handoff state for a run.

    Loads meta once to resolve the merged status; when the run is paused
    on ``awaiting_phase_handoff`` it layers the normalised
    :func:`project_handoff_read_model` on top and derives the round label
    + suggested next action. For any other status it returns a cheap
    ``is_pending_handoff=False`` projection so polling callers can skip
    the enrichment.

    Defensive: the meta read is wrapped in the shared SDK→MCP error
    owner; ``find_run_dir`` raises the typed ``RunNotFoundError`` when the
    run is missing (callers rely on that to keep resume's missing-run
    contract).
    """
    run_dir = find_run_dir(run_id)
    with map_sdk_errors(run_id):
        meta = _sdk_load_meta(run_dir) or {}
    status = merged_status_from_meta(meta, run_dir)

    if status != _PENDING_HANDOFF_STATUS:
        return PendingHandoffProjection(
            status=status, is_pending_handoff=False,
        )

    # Parse the operator fields straight off the payload we already loaded
    # — deliberately NOT via ``project_handoff_read_model``: that resolves
    # findings (with an SDK fallback) the compact pending summary does not
    # use, and re-loads meta. Findings stay an ``orcho_run_watch``
    # concern. This is the projection owner, so reading the
    # ``phase_handoff`` key here is sanctioned.
    handoff_payload = meta.get("phase_handoff") or {}
    if not isinstance(handoff_payload, dict):
        handoff_payload = {}

    actions = _normalise_handoff_actions(
        handoff_payload.get("available_actions"),
    )
    raw_id = handoff_payload.get("id")
    handoff_id = str(raw_id) if raw_id else None
    raw_phase = handoff_payload.get("phase")
    phase = (str(raw_phase) if raw_phase else None) or current_phase
    trigger = _normalise_handoff_trigger(handoff_payload.get("trigger"))
    verdict = _coerce_optional_str(handoff_payload.get("verdict"))
    round_n = _coerce_optional_int(handoff_payload.get("round"))
    loop_max_rounds = _coerce_optional_int(handoff_payload.get("loop_max_rounds"))
    last_output = _coerce_optional_str(handoff_payload.get("last_output"))
    decision_artifact_exists = _decision_artifact_exists(run_id, handoff_id)

    return PendingHandoffProjection(
        status=status,
        is_pending_handoff=True,
        handoff_id=handoff_id,
        phase=phase,
        trigger=trigger,
        verdict=verdict,
        round_label=_round_label(phase, round_n, loop_max_rounds),
        available_actions=actions,
        last_output=last_output,
        decision_artifact_exists=decision_artifact_exists,
        suggested_next_action=_suggested_next_action(
            decision_artifact_exists, actions,
        ),
    )


# ── Follow-up lineage projection ────────────────────────────────────────────
#
# Terminal-status predicate mirroring orcho-core's
# ``pipeline.control.resume_context.is_terminal_resume_parent`` — the
# canonical detector is pipeline-internal and not re-exported from the
# SDK (confirmed in the T0 audit), so the filter is replicated here from
# the persisted meta fields it reads (``status`` + ``halt_reason``). A
# child is an *active* follow-up exactly when it is NOT terminal by this
# predicate, matching ``detect_active_followup_child``.

_TERMINAL_SUCCESS_STATUSES = frozenset({"done", "success", "completed"})
_PHASE_HANDOFF_HALT_REASON = "phase_handoff_halt"
_COMMIT_DECISION_HALT_REASON = "commit_decision_halt"
_COMMIT_DECISION_FIX_REASON = "commit_decision_fix"
# Parked delivery-decision gates: a run that halted with its delivery decision
# still pending (defer mode) or with a strict-mono delivery-scope gate parked.
# Both await an out-of-band ``decide_delivery`` call, not a checkpoint
# continuation, so core's ``is_terminal_resume_parent`` treats them as
# terminal-for-checkpoint. The replica must include them too, otherwise a halted
# ``commit_delivery_*`` follow-up child would be mis-counted as an *active*
# follow-up here while core/CLI consider it non-checkpoint-resumable.
_COMMIT_DELIVERY_PENDING_REASON = "commit_delivery_pending"
_COMMIT_DELIVERY_SCOPE_BLOCKED_REASON = "commit_delivery_scope_blocked"
# Rejected final-acceptance dead-ends: a release the gate rejected with no
# applied delivery and no correction gate. ``final_acceptance_rejected`` is the
# rejected-override terminal; ``final_acceptance_no_diff`` fires when a rejecting
# run applied no diff at all. Both are pure terminal dead-ends (no operator
# decision pending), so checkpoint-resume must not select them. Value-mirror of
# orcho-core's ``resume_context.is_terminal_final_acceptance_rejected`` (added in
# the T1 core-vocabulary slice) — replicated here by VALUE, never by importing
# the pipeline-internal ``resume_context`` (core owns the protocol, this plugin
# keeps the replica).
_FINAL_ACCEPTANCE_REJECTED_REASON = "final_acceptance_rejected"
_FINAL_ACCEPTANCE_NO_DIFF_REASON = "final_acceptance_no_diff"
# Full value-mirror of orcho-core's ``resume_context.is_terminal_resume_parent``
# halt-reason set: phase-handoff halt, the two commit-decision halts, the two
# parked commit-delivery gates, and the two rejected final-acceptance dead-ends.
# Keep this in lockstep with core; never import the pipeline-internal predicate.
_TERMINAL_HALT_REASONS = frozenset({
    _PHASE_HANDOFF_HALT_REASON,
    _COMMIT_DECISION_HALT_REASON,
    _COMMIT_DECISION_FIX_REASON,
    _COMMIT_DELIVERY_PENDING_REASON,
    _COMMIT_DELIVERY_SCOPE_BLOCKED_REASON,
    _FINAL_ACCEPTANCE_REJECTED_REASON,
    _FINAL_ACCEPTANCE_NO_DIFF_REASON,
})


def _is_terminal_resume_parent(meta: dict) -> bool:
    """Whether checkpoint-resume should not select this run by default.

    Replicates ``resume_context.is_terminal_resume_parent``: terminal
    success (``done`` / ``success`` / ``completed``) or a ``halted`` run
    whose ``halt_reason`` is one of the terminal halt reasons (phase-handoff
    halt, the two commit-decision halts, the two parked commit-delivery
    gates, and the two rejected final-acceptance dead-ends). Every other
    status (``running`` / ``failed`` / ``interrupted`` / ``awaiting_*`` / a
    non-terminal ``halted``) is treated as resumable and therefore an
    *active* follow-up.
    """
    status = meta.get("status")
    if status in _TERMINAL_SUCCESS_STATUSES:
        return True
    if status == "halted":
        return meta.get("halt_reason") in _TERMINAL_HALT_REASONS
    return False


@dataclass(frozen=True)
class FollowupLineageProjection:
    """Follow-up lineage around a single run.

    ``parent_*`` describe the run this one continues (when it is itself a
    ``resume_mode='followup'`` child); the ``active_child_*`` fields and
    the recommendation describe the newest *unfinished* follow-up child of
    this run. When such a child exists, ``recommended_action`` is
    ``"resume_child"`` and ``recommended_run_id`` is the child to resume —
    the structured equivalent of the CLI's "Resume active follow-up
    <child>" guidance (resume the live child, not this parent).
    """

    run_id: str
    parent_run_id: str | None = None
    parent_status: str | None = None
    resume_mode: str | None = None
    has_active_child_followup: bool = False
    active_child_run_id: str | None = None
    active_child_status: str | None = None
    active_child_handoff_id: str | None = None
    recommended_action: str | None = None
    recommended_run_id: str | None = None
    recommendation: str | None = None


def _safe_run_status(run_id: str) -> str | None:
    """Merged status of ``run_id`` or ``None`` when it cannot be read.

    Defensive: a missing / unreadable parent must never break the lineage
    projection of the run being inspected.
    """
    try:
        run_dir = find_run_dir(run_id)
        meta = _sdk_load_meta(run_dir) or {}
        return merged_status_from_meta(meta, run_dir)
    except Exception:
        return None


def _detect_active_followup_child(
    run_id: str,
) -> tuple[str, str, str | None] | None:
    """Find the newest unfinished follow-up child of ``run_id``.

    Mirrors ``resume_context.detect_active_followup_child`` over the SDK
    readers: enumerate the runs dir, and a sibling qualifies when its
    ``meta.json`` records ``resume_mode == "followup"`` and
    ``parent_run_id == run_id``, it is not a cross sub-pipeline (no
    ``project_alias``), and it is not terminal
    (:func:`_is_terminal_resume_parent`). Returns
    ``(child_run_id, child_status, active_handoff_id)`` for the newest
    match by run id, or ``None``.

    Read-only: ``runs_dir_or_raise`` + ``sdk.load_meta`` per entry — no
    bespoke run-discovery walk or meta re-parser. Any resolution error
    degrades to ``None`` so the projection never raises on a status read.
    """
    try:
        runs_dir = runs_dir_or_raise()
    except Exception:
        return None

    best_name: str | None = None
    best: tuple[str, str, str | None] | None = None
    for entry in sorted(runs_dir.iterdir()):
        if not entry.is_dir() or entry.name == run_id:
            continue
        if entry.name.startswith("."):
            continue
        child_meta = _sdk_load_meta(entry) or {}
        if not isinstance(child_meta, dict):
            continue
        if child_meta.get("resume_mode") != "followup":
            continue
        if child_meta.get("parent_run_id") != run_id:
            continue
        if child_meta.get("project_alias"):
            continue
        if _is_terminal_resume_parent(child_meta):
            continue
        if best_name is not None and entry.name <= best_name:
            continue
        raw_status = child_meta.get("status")
        child_status = raw_status if isinstance(raw_status, str) else "unknown"
        active = child_meta.get("phase_handoff")
        raw_hid = active.get("id") if isinstance(active, dict) else None
        handoff_id = raw_hid if isinstance(raw_hid, str) and raw_hid else None
        best_name = entry.name
        best = (entry.name, child_status, handoff_id)
    return best


def project_followup_lineage(run_id: str) -> FollowupLineageProjection:
    """Project the follow-up lineage around ``run_id`` for status reads.

    Reads the inspected run's ``parent_run_id`` / ``resume_mode`` from
    meta (resolving the parent's status when present), then scans for the
    newest unfinished follow-up child and, when one exists, attaches a
    ``resume_child`` recommendation. Purely read-only; the child scan
    swallows its own errors so a status read never fails on lineage.
    """
    run_dir = find_run_dir(run_id)
    with map_sdk_errors(run_id):
        meta = _sdk_load_meta(run_dir) or {}

    resume_mode = _coerce_optional_str(meta.get("resume_mode"))
    parent_run_id = _coerce_optional_str(meta.get("parent_run_id"))
    parent_status = _safe_run_status(parent_run_id) if parent_run_id else None

    child = _detect_active_followup_child(run_id)
    if child is None:
        return FollowupLineageProjection(
            run_id=run_id,
            parent_run_id=parent_run_id,
            parent_status=parent_status,
            resume_mode=resume_mode,
        )

    child_run_id, child_status, child_handoff_id = child
    recommendation = (
        f"Resume active follow-up {child_run_id} instead of this run — a "
        "newer unfinished follow-up child is continuing this run's change "
        "session; resuming the parent would diverge from it."
    )
    return FollowupLineageProjection(
        run_id=run_id,
        parent_run_id=parent_run_id,
        parent_status=parent_status,
        resume_mode=resume_mode,
        has_active_child_followup=True,
        active_child_run_id=child_run_id,
        active_child_status=child_status,
        active_child_handoff_id=child_handoff_id,
        recommended_action="resume_child",
        recommended_run_id=child_run_id,
        recommendation=recommendation,
    )


# ── Worktree-continuity projection ──────────────────────────────────────────
#
# orcho-core persists the diff-aware follow-up worktree decision under
# ``meta['worktree']['followup_continuity']`` =
# ``{mode_label, blocked, reason, diff_source}`` (T0 audit confirmed the
# write at ``isolation_setup.py:191,343`` via ``save_session``;
# ``FollowupWorktreeDecision.to_dict``). ``diff_source`` is one of
# ``worktree`` / ``artifact`` / ``none``. A run that is NOT a follow-up
# has the ``worktree`` block but no ``followup_continuity`` sub-block — it
# kept its own worktree (same-run retained). This projection maps those
# persisted states into one normalised ``subject_mode`` plus the raw
# ``diff_source`` / ``block_message`` so a client never parses logs.


@dataclass(frozen=True)
class WorktreeContinuityProjection:
    """Normalised worktree-continuity state from ``meta['worktree']``.

    ``subject_mode`` is the operator-facing classification:

    - ``same_run_retained`` — not a follow-up; the run kept its own
      worktree (a fresh / checkpoint-resumed / provider-fallback run,
      where the same-run worktree is preserved);
    - ``reused_parent`` — a follow-up that reused the parent's dirty
      worktree, carrying the parent's uncommitted diff into the child
      (``diff_source='worktree'``);
    - ``clean_head_no_undelivered_diff`` — a follow-up that started fresh
      from HEAD because the parent had no undelivered diff
      (``diff_source='none'``);
    - ``blocked_parent_diff_unavailable`` — a follow-up blocked before any
      write phase because the parent's undelivered diff exists only as a
      ``diff.patch`` artifact this run will not replay
      (``diff_source='artifact'``); ``block_message`` carries the
      core warning that the change would be silently dropped on a clean
      HEAD and that resuming the parent recovers it;
    - ``unknown_continuity`` — a follow-up block with an unrecognised
      ``diff_source`` (defensive).

    ``worktree_preserved`` is ``True`` whenever a usable worktree exists
    (every mode except ``blocked``), so a same-run provider fallback
    reports the worktree as preserved.
    """

    has_worktree: bool
    subject_mode: str | None
    isolation: str | None
    path: str | None
    diff_source: str | None
    blocked: bool
    block_message: str | None
    mode_label: str | None
    worktree_preserved: bool
    degraded_reason: str | None
    is_followup_continuity: bool


def _worktree_subject_mode(diff_source: str | None, blocked: bool) -> str:
    """Map the persisted ``followup_continuity`` shape to a subject mode."""
    if blocked or diff_source == "artifact":
        return "blocked_parent_diff_unavailable"
    if diff_source == "worktree":
        return "reused_parent"
    if diff_source == "none":
        return "clean_head_no_undelivered_diff"
    return "unknown_continuity"


def project_worktree_continuity(
    worktree_meta: object,
) -> WorktreeContinuityProjection:
    """Project ``meta['worktree']`` into the normalised continuity model.

    Pure transform of the already-loaded ``worktree`` sub-dict — no IO, no
    SDK call. A missing / non-dict block yields ``has_worktree=False``; a
    block without ``followup_continuity`` is a non-follow-up run that kept
    its worktree (``same_run_retained``); otherwise the follow-up decision
    is classified from ``diff_source`` / ``blocked`` and the core warning
    is surfaced verbatim as ``block_message``.
    """
    if not isinstance(worktree_meta, dict) or not worktree_meta:
        return WorktreeContinuityProjection(
            has_worktree=False,
            subject_mode=None,
            isolation=None,
            path=None,
            diff_source=None,
            blocked=False,
            block_message=None,
            mode_label=None,
            worktree_preserved=False,
            degraded_reason=None,
            is_followup_continuity=False,
        )

    isolation = _coerce_optional_str(worktree_meta.get("isolation"))
    path = _coerce_optional_str(worktree_meta.get("path"))
    degraded_reason = _coerce_optional_str(worktree_meta.get("degraded_reason"))

    fc = worktree_meta.get("followup_continuity")
    if not isinstance(fc, dict):
        return WorktreeContinuityProjection(
            has_worktree=True,
            subject_mode="same_run_retained",
            isolation=isolation,
            path=path,
            diff_source=None,
            blocked=False,
            block_message=None,
            mode_label=None,
            worktree_preserved=True,
            degraded_reason=degraded_reason,
            is_followup_continuity=False,
        )

    blocked = bool(fc.get("blocked"))
    diff_source = _coerce_optional_str(fc.get("diff_source"))
    block_message = _coerce_optional_str(fc.get("reason"))
    mode_label = _coerce_optional_str(fc.get("mode_label"))
    return WorktreeContinuityProjection(
        has_worktree=True,
        subject_mode=_worktree_subject_mode(diff_source, blocked),
        isolation=isolation,
        path=path,
        diff_source=diff_source,
        blocked=blocked,
        block_message=block_message,
        mode_label=mode_label,
        worktree_preserved=not blocked,
        degraded_reason=degraded_reason,
        is_followup_continuity=True,
    )


# ── Provider-session fallback projection ────────────────────────────────────
#
# orcho-core emits a ``phase.provider_session_fallback`` event
# (``session_invoke.py``) ONLY after a fresh-session retry has already
# succeeded — the original missing-provider-session exception was caught
# and fully handled, and no ``failed`` record leaks out. The payload is
# ``{phase, stale_session_id, recovered=True}`` and the run keeps the same
# worktree + persisted context. This projection turns that event into
# structured fields and, crucially, reports ``phase_succeeded=True`` so a
# recovered fallback is never mistaken for a phase failure.

_PROVIDER_SESSION_FALLBACK_KIND = "phase.provider_session_fallback"
_FRESH_PROVIDER_SESSION_MODE = "fresh_provider_session"
_SESSION_ID_VISIBLE_PREFIX = 8


@dataclass(frozen=True)
class ProviderSessionFallbackProjection:
    """Structured view of one ``phase.provider_session_fallback`` event.

    ``stale_session_id`` is redacted (a short prefix + ``…``) so the full
    provider conversation id is never surfaced. ``fallback_mode`` is the
    fixed ``fresh_provider_session`` recovery; ``worktree_preserved`` is
    ``True`` because the fallback continues in the same run worktree with
    persisted context; ``phase_succeeded`` reflects the event's
    ``recovered`` flag — the event is only emitted on a successful retry,
    so a recovered fallback is a recovery notice, not a phase failure.
    """

    phase: str | None
    stale_session_id: str | None
    fallback_mode: str
    worktree_preserved: bool
    phase_succeeded: bool


def _redact_session_id(raw: object) -> str | None:
    """Redact a provider session id to a short non-reversible preview.

    ``None`` / empty → ``None``; the sentinel ``"unknown"`` (core's
    placeholder for a missing id) passes through unchanged. Otherwise a
    leading slice plus ``…`` is returned — never the full id, even for a
    short id (where only the first half is shown).
    """
    if raw is None:
        return None
    s = str(raw)
    if not s:
        return None
    if s == "unknown":
        return "unknown"
    visible = _SESSION_ID_VISIBLE_PREFIX
    if len(s) <= visible:
        visible = max(1, len(s) // 2)
    return s[:visible] + "…"


def project_provider_session_fallback(
    payload: object,
) -> ProviderSessionFallbackProjection:
    """Project one ``phase.provider_session_fallback`` event payload.

    Pure transform of the event payload dict — no IO. ``recovered`` is
    coerced to ``phase_succeeded`` (defaulting to ``True`` when absent,
    since core only emits this event after a successful fresh-session
    retry); ``stale_session_id`` is redacted.
    """
    data = payload if isinstance(payload, dict) else {}
    recovered = data.get("recovered")
    phase_succeeded = bool(recovered) if recovered is not None else True
    return ProviderSessionFallbackProjection(
        phase=_coerce_optional_str(data.get("phase")),
        stale_session_id=_redact_session_id(data.get("stale_session_id")),
        fallback_mode=_FRESH_PROVIDER_SESSION_MODE,
        worktree_preserved=True,
        phase_succeeded=phase_succeeded,
    )


def is_provider_session_fallback_event(kind: object) -> bool:
    """Whether an event ``kind`` is a provider-session fallback notice."""
    return kind == _PROVIDER_SESSION_FALLBACK_KIND


# ── Human-retry / repeated-reject projection ────────────────────────────────
#
# A handoff loop's reject lifecycle is derivable from raw persisted inputs
# (T0 decision — derive from raw fields, do NOT import
# ``pipeline.control.handoff_labels``):
#
# - ``meta.phase_handoff`` carries ``round`` / ``loop_max_rounds`` /
#   ``verdict`` / ``approved`` while paused. ``round > loop_max_rounds`` is
#   the structural marker of a human-directed retry round (the one-shot
#   ``retry_feedback`` rounds sit on top of the auto budget), mirroring
#   ``is_human_directed_round`` without importing it.
# - ``sdk.load_phase_handoff_decisions`` yields the persisted decisions;
#   an ``action == "retry_feedback"`` decision is the durable record that
#   the operator injected a human-directed retry (and carries the feedback
#   text).
#
# The four lifecycle states the projection distinguishes:
#   * ``automatic_reject`` — paused on a rejected verdict at an automatic
#     round (``round <= loop_max_rounds``); the operator must decide.
#   * ``human_retry_in_progress`` — a ``retry_feedback`` decision exists and
#     the run is running (the human-directed round is executing); best-effort
#     lifecycle hint.
#   * ``retry_rejected_again`` — paused again on a rejected verdict at a
#     human-directed round (``round > loop_max_rounds``); the human retry
#     was rejected and a fresh operator decision is required.
#   * ``retry_accepted_closed`` — a ``retry_feedback`` decision exists and the
#     run reached terminal success (the retry was accepted, handoff closed);
#     best-effort lifecycle hint.

_RETRY_FEEDBACK_ACTION = "retry_feedback"


@dataclass(frozen=True)
class RetryStateProjection:
    """Structured human-retry / repeated-reject lifecycle state.

    ``operator_feedback`` is the *raw* feedback from the most recent
    ``retry_feedback`` decision (presentation truncates it into a
    preview). ``pending_operator_decision`` is True only while the run is
    paused awaiting a decision on the active handoff (no decision artifact
    recorded for it yet).
    """

    retry_context: str
    retry_attempt_label: str | None
    operator_feedback: str | None
    pending_operator_decision: bool


def _load_handoff_decisions(run_id: str) -> list[object]:
    """Load persisted handoff decisions, swallowing read errors to ``[]``.

    Defensive: a missing / corrupt decisions directory must never break a
    status read that only wants the retry-lifecycle hint.
    """
    try:
        return list(_sdk_load_phase_handoff_decisions(run_id, cwd=None))
    except Exception:
        return []


def _retry_attempt_label(
    phase: str | None,
    round_n: int | None,
    loop_max_rounds: int | None,
    *,
    rejected: bool,
    retry_context: str,
) -> str | None:
    """Coherent operator label for the retry attempt (derived, never piped).

    Mirrors the three shapes of ``render_round_label`` from the raw round
    counters — never an impossible ``R/M`` with ``R > M`` — plus the two
    non-paused lifecycle hints. No ``pipeline.control`` import.
    """
    label_phase = phase or "phase"
    if retry_context == "human_retry_in_progress":
        return f"{label_phase} human retry in progress"
    if retry_context == "retry_accepted_closed":
        return f"{label_phase} human retry accepted; handoff closed"
    if round_n is None or loop_max_rounds is None or loop_max_rounds < 1:
        return None
    if round_n > loop_max_rounds:
        retry_k = max(1, round_n - loop_max_rounds)
        if rejected:
            return (
                f"{label_phase} human retry {retry_k} rejected; "
                "operator decision required"
            )
        return f"{label_phase} human retry {retry_k} after REJECTED verdict"
    shown = round_n if round_n >= 1 else 1
    return f"{label_phase} automatic round {shown}/{loop_max_rounds}"


def _is_rejected(verdict: str | None, approved: object) -> bool:
    """Whether a handoff payload represents a rejected verdict."""
    if approved is False:
        return True
    return isinstance(verdict, str) and verdict.strip().upper() == "REJECTED"


def project_retry_state(
    run_id: str, *, current_phase: str | None = None,
) -> RetryStateProjection | None:
    """Project the human-retry / repeated-reject lifecycle state for a run.

    Returns ``None`` when the run is not in a reject / retry lifecycle
    (no rejected pause and no ``retry_feedback`` decision). Otherwise
    classifies into one of the four ``retry_context`` states. Purely
    read-only; decision reads swallow their own errors.
    """
    run_dir = find_run_dir(run_id)
    with map_sdk_errors(run_id):
        meta = _sdk_load_meta(run_dir) or {}
    status = merged_status_from_meta(meta, run_dir)
    active = meta.get("phase_handoff") if isinstance(meta, dict) else None
    active = active if isinstance(active, dict) else None

    decisions = _load_handoff_decisions(run_id)
    retry_decisions = [
        d for d in decisions
        if getattr(d, "action", None) == _RETRY_FEEDBACK_ACTION
    ]
    operator_feedback = (
        getattr(retry_decisions[-1], "feedback", None)
        if retry_decisions else None
    )
    decided_ids = {
        getattr(d, "handoff_id", None) for d in decisions
    }

    paused = status == _PENDING_HANDOFF_STATUS

    if paused and active is not None:
        round_n = _coerce_optional_int(active.get("round"))
        loop_max = _coerce_optional_int(active.get("loop_max_rounds"))
        verdict = _coerce_optional_str(active.get("verdict"))
        rejected = _is_rejected(verdict, active.get("approved"))
        phase = (
            _coerce_optional_str(active.get("phase")) or current_phase
        )
        active_id = _coerce_optional_str(active.get("id"))
        human_directed = (
            round_n is not None
            and loop_max is not None
            and round_n > loop_max
        )
        if not rejected:
            # Paused but not a reject (e.g. an always-policy approved
            # pause) — not part of the reject / retry lifecycle.
            return None
        retry_context = (
            "retry_rejected_again" if human_directed else "automatic_reject"
        )
        pending = (active_id not in decided_ids) if active_id else True
        return RetryStateProjection(
            retry_context=retry_context,
            retry_attempt_label=_retry_attempt_label(
                phase, round_n, loop_max,
                rejected=rejected, retry_context=retry_context,
            ),
            operator_feedback=operator_feedback,
            pending_operator_decision=pending,
        )

    # Not paused on a handoff. Only a recorded human retry makes this a
    # retry lifecycle; classify the (best-effort) running / closed hint.
    if not retry_decisions:
        return None
    phase = getattr(retry_decisions[-1], "phase", None) or current_phase
    if status in _TERMINAL_SUCCESS_STATUSES:
        retry_context = "retry_accepted_closed"
    elif status == "running":
        retry_context = "human_retry_in_progress"
    else:
        # Terminal failure / halt after a retry — do not mislabel as an
        # accepted close; the reject lifecycle did not resolve cleanly.
        return None
    return RetryStateProjection(
        retry_context=retry_context,
        retry_attempt_label=_retry_attempt_label(
            phase, None, None, rejected=False, retry_context=retry_context,
        ),
        operator_feedback=operator_feedback,
        pending_operator_decision=False,
    )


# ── Terminal-consistency projection ─────────────────────────────────────────
#
# The owner of "is this terminal run coherent, and is resuming it
# meaningful?" — the narrow read of ``meta.phases.final_acceptance`` plus
# the merged status / halt_reason, used by the live-status card. The
# final-acceptance verdict is reconstructed from durable meta without any
# orcho-core change: core persists the gate verdict at
# ``meta.phases.final_acceptance.verdict`` (with an ``approved`` bool
# companion), confirmed by the ``meta_summary`` gate-field projection.
# ``meta.phase_handoff`` is NOT read here — only the final-acceptance phase.

# Final-acceptance terminal failure statuses that still warrant a resume
# pointer (mirrors observe.summary's terminal-failure vocabulary). A
# ``halted`` run carries its halt_reason; the others are resumable.
_TERMINAL_FAILURE_STATUSES = frozenset({
    "failed", "interrupted", "halted", "orphaned",
})

# The single terminal contradiction this projection detects: a terminal
# *success* status while the final-acceptance gate recorded a rejection.
# A halted run with a rejection is coherent (it halted *because* of the
# rejection) and is NOT flagged here.
_INCONSISTENCY_DONE_BUT_REJECTED = (
    "status_terminal_success_but_final_acceptance_rejected"
)


@dataclass(frozen=True)
class TerminalConsistencyProjection:
    """Terminal coherence + resume-meaningfulness for a run.

    ``final_acceptance_verdict`` is the normalised gate verdict
    (``APPROVED`` / ``REJECTED``) read from
    ``meta.phases.final_acceptance`` — ``None`` when the run has no
    final-acceptance phase. ``final_acceptance_summary`` is the *raw*
    short summary (presentation truncates it into a preview).

    ``inconsistencies`` carries the single detected contradiction
    (terminal success + rejection); it is empty for a coherent terminal.
    ``resume_meaningful`` is ``False`` only for a clean terminal success
    (resume would be inert) and ``True`` for every halted / awaiting /
    non-terminal state.
    """

    status: str | None
    halt_reason: str | None
    is_terminal_success: bool
    is_halted: bool
    final_acceptance_verdict: str | None
    final_acceptance_rejected: bool
    final_acceptance_summary: str | None
    resume_meaningful: bool
    inconsistencies: list[str] = field(default_factory=list)
    # Correction-followup contract: child run id when this run was superseded by a successful
    # correction follow-up (durable ``superseded_by_followup`` marker). When
    # set, a ``done`` + historically-REJECTED final acceptance is NOT a live
    # contradiction — the rejection was resolved by the follow-up — so the
    # done-but-rejected inconsistency is suppressed and the run reads as closed.
    superseded_by_followup: str | None = None


def _final_acceptance(meta: object) -> tuple[str | None, bool, str | None]:
    """Read ``meta.phases.final_acceptance`` → (verdict, rejected, summary).

    Pure transform of the already-loaded ``meta`` dict — no IO. The
    verdict is normalised to upper-case; when the payload carries only the
    ``approved`` bool it is derived from that. ``rejected`` is ``True`` for
    a ``REJECTED`` verdict or an explicit ``approved is False``. A missing
    / malformed final-acceptance block yields ``(None, False, None)``.
    """
    phases = meta.get("phases") if isinstance(meta, dict) else None
    fa = phases.get("final_acceptance") if isinstance(phases, dict) else None
    if not isinstance(fa, dict):
        return None, False, None
    raw_verdict = fa.get("verdict")
    verdict = (
        raw_verdict.strip().upper()
        if isinstance(raw_verdict, str) and raw_verdict.strip() else None
    )
    approved = fa.get("approved")
    if verdict is None and isinstance(approved, bool):
        verdict = "APPROVED" if approved else "REJECTED"
    rejected = verdict == "REJECTED" or approved is False
    summary = _coerce_optional_str(fa.get("short_summary"))
    return verdict, rejected, summary


def project_terminal_consistency(run_id: str) -> TerminalConsistencyProjection:
    """Project a run's terminal coherence + resume-meaningfulness.

    Loads meta once for the merged status / halt_reason and the narrow
    ``meta.phases.final_acceptance`` read, then detects the single
    terminal contradiction (a terminal success status while the gate
    recorded a rejection) and derives ``resume_meaningful``. Purely
    read-only; ``find_run_dir`` propagates the typed ``RunNotFoundError``
    so the missing-run contract matches the other readers.
    """
    run_dir = find_run_dir(run_id)
    with map_sdk_errors(run_id):
        meta = _sdk_load_meta(run_dir) or {}
    status = merged_status_from_meta(meta, run_dir)
    halt_reason = merged_halt_reason_from_meta(meta, run_dir)
    verdict, rejected, summary = _final_acceptance(meta)

    is_terminal_success = status in _TERMINAL_SUCCESS_STATUSES
    is_halted = status == "halted"
    superseded_child = _superseded_followup_child(meta)

    inconsistencies: list[str] = []
    # A superseded parent's historical REJECTED verdict is not a live
    # contradiction — the follow-up resolved it — so the
    # done-but-rejected inconsistency is suppressed for it.
    if is_terminal_success and rejected and not superseded_child:
        inconsistencies.append(_INCONSISTENCY_DONE_BUT_REJECTED)

    # A clean terminal success is inert to resume; every halted / awaiting /
    # non-terminal state can still make progress. A done+reject contradiction
    # stays inert (it is recorded as terminal success) — the contradiction is
    # surfaced via ``inconsistencies``, not by pretending resume helps.
    resume_meaningful = not is_terminal_success

    return TerminalConsistencyProjection(
        status=status,
        halt_reason=halt_reason,
        is_terminal_success=is_terminal_success,
        is_halted=is_halted,
        final_acceptance_verdict=verdict,
        final_acceptance_rejected=rejected,
        final_acceptance_summary=summary,
        resume_meaningful=resume_meaningful,
        inconsistencies=inconsistencies,
        superseded_by_followup=superseded_child,
    )


# ── Unified run-diagnosis projection ────────────────────────────────────────
#
# The single typed classifier shared by ``orcho_run_diagnose`` (GC-1) and the
# resume pre-flight guard (GC-2). It composes the existing projections — never
# re-deriving terminality or lineage predicates — into one ``condition`` plus a
# fact-built ``reason`` (no prose parsing). Priority is deterministic: the first
# matching branch wins, so an ``awaiting_phase_handoff`` pause is reported as a
# decision even when a stale terminal halt sits underneath, and a live follow-up
# child supersedes an otherwise-inert terminal parent.

# Diagnosis condition labels — one per priority branch (plus the residual
# resumable statuses, which surface their own status string as the condition).
_CONDITION_NEEDS_DECISION = "needs_decision"
_CONDITION_NEEDS_DELIVERY_DECISION = "needs_delivery_decision"
# Correction-followup contract: a correction whose ``fix`` was already requested (or a rejected
# dead-end). Resuming THIS run is inert and a repeated ``fix`` is a no-op; the
# actionable next step is a correction follow-up over the retained worktree.
_CONDITION_CORRECTION_FOLLOWUP_REQUIRED = "correction_followup_required"
_CONDITION_SUPERSEDED_BY_CHILD = "superseded_by_child"
# Correction-followup contract: a rejected-FA / correction parent that a successful correction
# follow-up child has CLOSED (core finalization stamps ``superseded_by_followup``
# and settles the parent to ``done``). It is terminal and resume is inert, but —
# unlike a plain ``resume_inert_terminal`` dead-end — it is explicitly a settled
# success: a distinct typed condition so clients can tell a closed-by-correction
# parent from any other inert terminal and never read its old release_blockers
# as authoritative.
_CONDITION_CLOSED_BY_FOLLOWUP = "closed_by_followup"
_CONDITION_BLOCKED_WORKTREE = "blocked_worktree"
_CONDITION_RECOVER_VIA_SOURCE_RUN = "recover_via_source_run"
_CONDITION_RESUME_INERT_TERMINAL = "resume_inert_terminal"
_CONDITION_ACTIVE = "active"

# Conditions where core's ``run_diagnosis`` already encodes the correct
# terminal / decision / recover answer. The post-core rejected-dead-end
# reconciliation applies ONLY to the residual / active conditions OUTSIDE this
# set: a closed REJECTED release with a non-terminal ``halt_reason`` that core
# leaves in the residual ``halted`` / ``failed`` branch, yet whose attached
# ``recovery.is_terminal_or_rejected`` flags it as an inert dead-end. We consume
# core's own recovery signal to upgrade it to ``resume_inert_terminal`` — never
# re-deriving terminality (no ``_is_terminal_resume_parent`` call here).
_CORE_RESOLVED_CONDITIONS = frozenset({
    _CONDITION_NEEDS_DECISION,
    _CONDITION_SUPERSEDED_BY_CHILD,
    _CONDITION_BLOCKED_WORKTREE,
    _CONDITION_NEEDS_DELIVERY_DECISION,
    _CONDITION_CORRECTION_FOLLOWUP_REQUIRED,
    _CONDITION_RECOVER_VIA_SOURCE_RUN,
    _CONDITION_RESUME_INERT_TERMINAL,
    _CONDITION_CLOSED_BY_FOLLOWUP,
})


@dataclass(frozen=True)
class RunDiagnosisProjection:
    """Typed, deterministic classification of a run's resume situation.

    ``condition`` is the first matching branch in the fixed priority order
    (``needs_decision`` → ``superseded_by_child`` → ``blocked_worktree`` →
    ``resume_inert_terminal`` → ``active`` → the residual resumable status).
    ``reason`` is a single line assembled from persisted facts (status,
    halt_reason, ids) — never parsed from log prose.

    The ``recommended_*`` / ``handoff_id`` / ``available_actions`` /
    ``parent_run_id`` fields carry the branch-specific guidance the wire
    layers (resume outcome, diagnose next-actions) build on, and stay
    ``None`` / empty for branches that do not populate them.
    """

    condition: str
    reason: str
    run_id: str
    status: str | None
    halt_reason: str | None
    recommended_run_id: str | None = None
    recommended_action: str | None = None
    handoff_id: str | None = None
    available_actions: list[str] = field(default_factory=list)
    parent_run_id: str | None = None
    blocked: bool = False
    block_message: str | None = None
    delivery_gate_kind: str | None = None
    # ``needs_decision`` enrichment: whether a phase-handoff decision artifact
    # already exists for the active ``handoff_id`` (read off the shared
    # ``project_pending_handoff`` projection — the single source of truth, so
    # diagnose / live_status / summary never diverge). ``True`` flips the
    # diagnose routing from decide verbs to a ready resume.
    decision_artifact_exists: bool = False
    # Recovery-lineage enrichment (composed from ``project_recovery_lineage``).
    # ``continuation_subject`` / ``recommended_next_action`` are the typed
    # continuation vocabulary; ``source_run_id`` / ``missing_facts`` carry the
    # source pointer + the durable facts a dead-end lacks; ``recovery_lineage``
    # is the full structured projection the wire layer maps into its submodel.
    continuation_subject: str | None = None
    recommended_next_action: str | None = None
    source_run_id: str | None = None
    missing_facts: list[str] = field(default_factory=list)
    recovery_lineage: RecoveryLineageProjection | None = None
    # ``correction_followup_required`` enrichment: the target checkout
    # retained-worktree facts. The wire layer emits the core-owned typed
    # ``orcho_run_resume`` input requirement. ``None`` for every other condition.
    followup_project_dir: str | None = None
    followup_diff_path: str | None = None
    followup_retained_worktree: str | None = None
    # Controllability axis (orthogonal to ``condition``): whether *this* MCP
    # server can mutate (resume / decide) the run, or can only inspect it.
    # ``mcp_controllable`` iff the run carries durable ``mcp_supervisor.json``
    # state with a resolvable project_dir (it was started by this server);
    # ``inspect_only`` for a foreign / CLI run dir. ``None`` when the durable
    # classification could not be read (never defaulted to controllable).
    # Overlaid once via :func:`project_run_control` in
    # :func:`project_run_diagnosis`, never per-branch.
    control: str | None = None
    control_reason: str | None = None


def _safe_pending_handoff(run_id: str) -> PendingHandoffProjection | None:
    """Project the pending-handoff state, swallowing read errors to ``None``.

    Defensive: the auxiliary handoff read must never turn a resolvable run
    into a new failure point for the diagnosis.
    """
    try:
        return project_pending_handoff(run_id)
    except Exception:
        return None


def _safe_followup_lineage(run_id: str) -> FollowupLineageProjection | None:
    """Project the follow-up lineage, swallowing read errors to ``None``.

    Defensive: lineage is auxiliary context for the diagnosis; a corrupt
    sibling or parent read must not break the classification of this run.
    """
    try:
        return project_followup_lineage(run_id)
    except Exception:
        return None


def _safe_run_control(run_id: str) -> RunControlProjection | None:
    """Project the durable controllability verdict, swallowing errors to ``None``.

    Defensive: the controllability axis is auxiliary enrichment of the
    diagnosis — an unreadable / unavailable classification must leave
    ``control`` unset (``None``) rather than block the diagnosis or default to a
    mutable verdict. A genuinely missing run still raises earlier from
    ``project_run_diagnosis``'s own ``find_run_dir``.
    """
    try:
        return project_run_control(run_id)
    except Exception:
        return None


def _safe_delivery_gate(run_id: str):
    """Project the delivery gate, swallowing read errors to ``None``.

    Reuses ``services.delivery_gate.project_delivery_gate`` so the
    pending-gate classification stays single-sourced (status-only) — the
    diagnosis never re-derives it. Defensive: an auxiliary delivery-gate
    read must never turn a resolvable run into a new failure point.
    """
    try:
        return project_delivery_gate(run_id)
    except Exception:
        return None


# Core ``RunDiagnosis.delivery_gate_kind`` emits the bare SDK gate kind
# (``delivery`` / ``correction``); the MCP wire vocabulary spells these as
# ``delivery_decision_required`` / ``correction_decision_required`` — the same
# strings ``services.delivery_gate._gate_kind_from_state`` projects. Keep one
# mapping so a gate-probe failure still surfaces core's published kind in the
# wire vocabulary rather than dropping it to ``None``.
_CORE_GATE_KIND_TO_WIRE = {
    "delivery": "delivery_decision_required",
    "correction": "correction_decision_required",
}


def _wire_delivery_gate_kind(gate: object, core_kind: str | None) -> str | None:
    """Resolve the wire ``delivery_gate_kind`` for a delivery/correction branch.

    The MCP gate probe (``gate.kind``) is the authoritative wire-vocab value and
    overrides whenever the probe resolved. When the probe is unavailable
    (``gate is None`` — a transient ``project_delivery_gate`` failure) the branch
    must still carry core's published ``delivery_gate_kind``, mapped from the bare
    SDK kind into the wire vocabulary, so the field is never lost while core knew
    it. An unrecognised core kind passes through unchanged (defensive).
    """
    if gate is not None:
        return gate.kind
    if core_kind is None:
        return None
    return _CORE_GATE_KIND_TO_WIRE.get(core_kind, core_kind)


def _held_diff_path(run_dir: Path) -> str | None:
    """Absolute path to ``run_dir/diff.patch`` when present, else ``None``.

    The retained diff is recovery context for a correction follow-up; a
    missing patch (or any stat failure) degrades to ``None`` rather than raising.
    """
    try:
        patch = run_dir / "diff.patch"
        return str(patch) if patch.is_file() else None
    except OSError:
        return None


def _superseded_followup_child(meta: object) -> str | None:
    """Child run id from a durable ``superseded_by_followup`` marker, else None.

    orcho-core's finalization stamps ``superseded_by_followup`` on a
    rejected-FA / correction parent once a correction follow-up child has
    delivered, settling the parent to ``done``. Its presence means the parent is
    closed/superseded — never an active correction candidate.
    """
    marker = meta.get("superseded_by_followup") if isinstance(meta, dict) else None
    if isinstance(marker, dict):
        child = marker.get("child_run_id")
        if isinstance(child, str) and child:
            return child
    return None


def _map_core_recovery(
    recovery: object,
) -> RecoveryLineageProjection | None:
    """Map core's attached ``RunDiagnosis.recovery`` to the wire projection.

    Reuses T1's field-for-field mapper (``services.run_lineage`` —
    ``RecoveryLineage`` → :class:`RecoveryLineageProjection`, ``missing_facts``
    tuple → list) so the diagnosis-attached recovery lineage and the standalone
    ``project_recovery_lineage`` map identically. Lazy-imported to break the
    module-load cycle; defensive so an import / mapping failure degrades the
    enrichment to ``None`` rather than breaking the diagnosis.
    """
    if recovery is None:
        return None
    try:
        from orcho_mcp.services.run_lineage import _project_recovery_lineage
        return _project_recovery_lineage(recovery)
    except Exception:
        return None


def _diagnosis_source_meta(
    meta: dict, parent_run_id: str | None,
) -> dict[str, dict]:
    """Build the ``{candidate_run_id: supervisor-merged meta}`` seam for core.

    The run-diagnosis counterpart of T1's recovery source_meta: the durable
    source pointers core's recovery resolver consults — the inspected run's
    ``parent_run_id`` (already resolved via the follow-up lineage) then its
    ``plan_source_run_id`` — each loaded once and supervisor-merged via the
    shared :func:`merged_meta`. Feeding the merged status keeps a stale on-disk
    ``status='running'`` source from forcing a blind ``recover_via_source_run``.
    A duplicate / unreadable candidate is skipped.
    """
    plan_source_run_id = _coerce_optional_str(meta.get("plan_source_run_id"))
    source_meta: dict[str, dict] = {}
    for candidate in (parent_run_id, plan_source_run_id):
        if not candidate or candidate in source_meta:
            continue
        merged = _safe_candidate_merged_meta(candidate)
        if merged is not None:
            source_meta[candidate] = merged
    return source_meta


def _safe_candidate_merged_meta(candidate_run_id: str) -> dict | None:
    """Load + supervisor-merge one source candidate's meta, or ``None``.

    Defensive: any read failure degrades to ``None`` so a corrupt source-meta
    cannot break the inspected run's diagnosis.
    """
    try:
        run_dir = find_run_dir(candidate_run_id)
        meta = _sdk_load_meta(run_dir) or {}
    except Exception:
        return None
    if not isinstance(meta, dict):
        return None
    return merged_meta(meta, run_dir)


def _resume_inert_terminal_projection(
    run_id: str,
    status: str | None,
    halt_reason: str | None,
    parent_run_id: str | None,
    *,
    reason: str,
    continuation_subject: str | None,
    recommended_next_action: str | None,
    missing_facts: list[str],
    source_run_id: str | None,
    recommended_run_id: str | None,
    recovery_lineage: RecoveryLineageProjection | None,
) -> RunDiagnosisProjection:
    """Build the ``resume_inert_terminal`` projection from resolved fields.

    Shared by core's own ``resume_inert_terminal`` branch (sourced from the
    ``RunDiagnosis`` fields) and the post-core rejected-dead-end reconciliation
    (sourced from core's attached ``recovery``), so both produce a byte-identical
    inert-terminal verdict. ``recommended_run_id`` is carried straight from core
    (it points at the plan-owning / source run a plan-artifact continuation
    should act on, and is ``None`` for a bare terminal dead-end) — never
    re-derived MCP-side.
    """
    return RunDiagnosisProjection(
        condition=_CONDITION_RESUME_INERT_TERMINAL,
        reason=reason,
        run_id=run_id,
        status=status,
        halt_reason=halt_reason,
        recommended_run_id=recommended_run_id,
        parent_run_id=parent_run_id,
        continuation_subject=continuation_subject,
        recommended_next_action=recommended_next_action,
        source_run_id=source_run_id,
        missing_facts=missing_facts,
        recovery_lineage=recovery_lineage,
    )


def project_run_diagnosis(run_id: str) -> RunDiagnosisProjection:
    """Classify a run's resume situation into one typed diagnosis.

    A thin projection of core's :func:`sdk.run_control.run_diagnosis`: it resolves
    the run dir (``find_run_dir`` propagates the typed ``RunNotFoundError`` so the
    missing-run contract is unchanged), supervisor-merges the inspected run's and
    each source candidate's status/halt_reason (:func:`merged_meta` /
    :func:`_diagnosis_source_meta`), and feeds those to core as ``meta=`` /
    ``source_meta=``. Core owns the condition / continuation-subject classification
    and the attached ``recovery`` lineage; this function maps ``RunDiagnosis`` →
    :class:`RunDiagnosisProjection` (incl. nested ``recovery_lineage``) and layers
    back the MCP-only enrichment core does not carry:

    - ``parent_run_id`` (follow-up lineage), on every branch;
    - ``decision_artifact_exists`` + the recorded-decision reason refinement
      (``needs_decision``);
    - ``recommended_action='resume_child'`` (``superseded_by_child``);
    - ``delivery_gate_kind`` in the MCP wire vocabulary
      (``{delivery,correction}_decision_required`` — core emits the bare
      ``delivery`` / ``correction`` kind) + ``followup_project_dir`` /
      ``followup_diff_path`` / ``followup_retained_worktree``
      (``correction_followup_required``), sourced from the delivery-gate probe.

    One reconciliation stays MCP-side because core's condition ladder does not
    carry it: a closed REJECTED release with a non-terminal ``halt_reason`` lands
    in core's residual ``halted`` / ``failed`` branch, but its attached
    ``recovery.is_terminal_or_rejected`` flags it as an inert dead-end — we
    upgrade it to ``resume_inert_terminal`` using core's own recovery signal
    (never re-deriving terminality).
    """
    run_dir = find_run_dir(run_id)
    with map_sdk_errors(run_id):
        meta = _sdk_load_meta(run_dir) or {}
    if not isinstance(meta, dict):
        meta = {}
    resolved_meta = merged_meta(meta, run_dir)

    lineage = _safe_followup_lineage(run_id)
    parent_run_id = lineage.parent_run_id if lineage else None
    source_meta = _diagnosis_source_meta(meta, parent_run_id)

    # ``cwd=None`` disables core's cwd walk-up so it resolves the runs dir from
    # ``$ORCHO_WORKSPACE`` exactly as MCP's ``find_run_dir`` does (the long-lived
    # server must never bind to whatever cwd it was launched from).
    diagnosis = _sdk_run_diagnosis(
        run_id, cwd=None, meta=resolved_meta, source_meta=source_meta,
    )
    proj = _project_run_diagnosis(run_id, run_dir, diagnosis, parent_run_id)

    # Overlay the durable controllability axis once, here — the single point so
    # no per-branch return has to thread it. Orthogonal to ``condition``: a run
    # can be ``active`` / ``needs_decision`` / terminal AND still be a foreign
    # ``inspect_only`` run this server cannot mutate. Defensive: when the
    # classification cannot be read leave ``control``/``control_reason`` at their
    # ``None`` defaults (never default to ``mcp_controllable``).
    control = _safe_run_control(run_id)
    if control is not None:
        proj = replace(
            proj, control=control.control, control_reason=control.reason,
        )
    return proj


def _project_run_diagnosis(
    run_id: str,
    run_dir: Path,
    diagnosis: object,
    parent_run_id: str | None,
) -> RunDiagnosisProjection:
    """Map core's ``RunDiagnosis`` → projection + MCP-only enrichment, per branch."""
    cond = diagnosis.condition
    status = diagnosis.status
    halt_reason = diagnosis.halt_reason
    recovery_lineage = _map_core_recovery(diagnosis.recovery)

    # (1) needs_decision — overlay decision_artifact_exists + the recorded reason.
    if cond == _CONDITION_NEEDS_DECISION:
        pending = _safe_pending_handoff(run_id)
        decision_recorded = bool(
            pending.decision_artifact_exists if pending else False
        )
        reason = diagnosis.reason
        if decision_recorded:
            # The status stays awaiting_phase_handoff; the recorded artifact
            # means the next step is resume, not a re-decide. Core does not read
            # the artifact, so this reason refinement is MCP-side.
            id_suffix = f" ({diagnosis.handoff_id})" if diagnosis.handoff_id else ""
            reason = (
                "run is paused awaiting a phase-handoff decision"
                f"{id_suffix}; decision recorded — resume to continue"
            )
        return RunDiagnosisProjection(
            condition=cond,
            reason=reason,
            run_id=run_id,
            status=status,
            halt_reason=halt_reason,
            handoff_id=diagnosis.handoff_id,
            available_actions=list(diagnosis.available_actions),
            parent_run_id=parent_run_id,
            decision_artifact_exists=decision_recorded,
            recovery_lineage=recovery_lineage,
        )

    # (2) superseded_by_child — overlay the MCP-only recommended_action. Core
    # publishes the live child's ``handoff_id``; carry it so the field is not
    # lost relative to the core read-model.
    if cond == _CONDITION_SUPERSEDED_BY_CHILD:
        return RunDiagnosisProjection(
            condition=cond,
            reason=diagnosis.reason,
            run_id=run_id,
            status=status,
            halt_reason=halt_reason,
            recommended_run_id=diagnosis.recommended_run_id,
            recommended_action="resume_child",
            handoff_id=diagnosis.handoff_id,
            parent_run_id=parent_run_id,
            continuation_subject=diagnosis.continuation_subject,
            recommended_next_action=diagnosis.recommended_next_action,
            recovery_lineage=recovery_lineage,
        )

    # (3) blocked_worktree.
    if cond == _CONDITION_BLOCKED_WORKTREE:
        return RunDiagnosisProjection(
            condition=cond,
            reason=diagnosis.reason,
            run_id=run_id,
            status=status,
            halt_reason=halt_reason,
            recommended_run_id=diagnosis.recommended_run_id,
            parent_run_id=parent_run_id,
            blocked=diagnosis.blocked,
            block_message=diagnosis.block_message,
            continuation_subject=diagnosis.continuation_subject,
            recommended_next_action=diagnosis.recommended_next_action,
            recovery_lineage=recovery_lineage,
        )

    # (4) needs_delivery_decision / correction_followup_required. The condition
    # is core-owned; the MCP gate probe supplies the wire-vocabulary
    # ``delivery_gate_kind`` (core emits the bare ``delivery`` / ``correction``
    # kind) and the correction-followup enrichment fields. When the probe is
    # unavailable the wire ``delivery_gate_kind`` falls back to core's published
    # kind (mapped to the wire vocabulary) so it is never lost — see
    # ``_wire_delivery_gate_kind``.
    if cond in (
        _CONDITION_NEEDS_DELIVERY_DECISION,
        _CONDITION_CORRECTION_FOLLOWUP_REQUIRED,
    ):
        gate = _safe_delivery_gate(run_id)
        delivery_gate_kind = _wire_delivery_gate_kind(
            gate, diagnosis.delivery_gate_kind,
        )
        if cond == _CONDITION_CORRECTION_FOLLOWUP_REQUIRED:
            return RunDiagnosisProjection(
                condition=cond,
                reason=diagnosis.reason,
                run_id=run_id,
                status=status,
                halt_reason=halt_reason,
                available_actions=list(diagnosis.available_actions),
                parent_run_id=parent_run_id,
                delivery_gate_kind=delivery_gate_kind,
                continuation_subject=diagnosis.continuation_subject,
                recommended_next_action=diagnosis.recommended_next_action,
                followup_project_dir=gate.target_checkout if gate else None,
                followup_diff_path=_held_diff_path(run_dir),
                followup_retained_worktree=(
                    gate.retained_worktree if gate else None
                ),
                recovery_lineage=recovery_lineage,
            )
        return RunDiagnosisProjection(
            condition=cond,
            reason=diagnosis.reason,
            run_id=run_id,
            status=status,
            halt_reason=halt_reason,
            recommended_run_id=diagnosis.recommended_run_id,
            available_actions=list(diagnosis.available_actions),
            parent_run_id=parent_run_id,
            delivery_gate_kind=delivery_gate_kind,
            continuation_subject=diagnosis.continuation_subject,
            recommended_next_action=diagnosis.recommended_next_action,
            recovery_lineage=recovery_lineage,
        )

    # (5) recover_via_source_run — resume the source checkpoint, not this run.
    if cond == _CONDITION_RECOVER_VIA_SOURCE_RUN:
        return RunDiagnosisProjection(
            condition=cond,
            reason=diagnosis.reason,
            run_id=run_id,
            status=status,
            halt_reason=halt_reason,
            recommended_run_id=diagnosis.recommended_run_id,
            parent_run_id=parent_run_id,
            continuation_subject=diagnosis.continuation_subject,
            recommended_next_action=diagnosis.recommended_next_action,
            source_run_id=diagnosis.source_run_id,
            recovery_lineage=recovery_lineage,
        )

    # (6) closed_by_followup — a parent CLOSED by a successful follow-up child.
    if cond == _CONDITION_CLOSED_BY_FOLLOWUP:
        return RunDiagnosisProjection(
            condition=cond,
            reason=diagnosis.reason,
            run_id=run_id,
            status=status,
            halt_reason=halt_reason,
            recommended_run_id=diagnosis.recommended_run_id,
            parent_run_id=parent_run_id,
            continuation_subject=diagnosis.continuation_subject,
            recommended_next_action=diagnosis.recommended_next_action,
            source_run_id=diagnosis.source_run_id,
            recovery_lineage=recovery_lineage,
        )

    # (7) resume_inert_terminal — core flagged a hard terminal dead-end.
    if cond == _CONDITION_RESUME_INERT_TERMINAL:
        return _resume_inert_terminal_projection(
            run_id, status, halt_reason, parent_run_id,
            reason=diagnosis.reason,
            continuation_subject=diagnosis.continuation_subject,
            recommended_next_action=diagnosis.recommended_next_action,
            missing_facts=list(diagnosis.missing_facts),
            source_run_id=diagnosis.source_run_id,
            recommended_run_id=diagnosis.recommended_run_id,
            recovery_lineage=recovery_lineage,
        )

    # (8) Post-core reconciliation: a closed REJECTED release with a non-terminal
    # halt_reason lands in core's residual ``halted`` / ``failed`` branch, yet its
    # attached recovery flags it terminal/rejected. Upgrade it to
    # resume_inert_terminal using core's OWN recovery signal (no terminality
    # re-derivation), so a rejected dead-end never offers a resume of itself.
    recovery = diagnosis.recovery
    if (
        cond not in _CORE_RESOLVED_CONDITIONS
        and recovery is not None
        and recovery.is_terminal_or_rejected
    ):
        return _resume_inert_terminal_projection(
            run_id, status, halt_reason, parent_run_id,
            reason=recovery.reason,
            continuation_subject=recovery.continuation_subject,
            recommended_next_action=recovery.recommended_next_action,
            missing_facts=list(recovery.missing_facts),
            source_run_id=recovery.source_run_id,
            recommended_run_id=recovery.recommended_run_id,
            recovery_lineage=recovery_lineage,
        )

    # (9) active — the run is currently executing.
    if cond == _CONDITION_ACTIVE:
        return RunDiagnosisProjection(
            condition=cond,
            reason=diagnosis.reason,
            run_id=run_id,
            status=status,
            halt_reason=halt_reason,
            parent_run_id=parent_run_id,
            recovery_lineage=recovery_lineage,
        )

    # (10) residual resumable — the status itself is the condition. Core sets a
    # ``none`` continuation_subject here; the pre-migration projection left it
    # ``None`` (a resumable run continues itself), so we do not carry core's
    # sentinel — the run simply resumes itself.
    return RunDiagnosisProjection(
        condition=cond,
        reason=diagnosis.reason,
        run_id=run_id,
        status=status,
        halt_reason=halt_reason,
        parent_run_id=parent_run_id,
        recovery_lineage=recovery_lineage,
    )


# ── Auto-detect profile-selector projection ─────────────────────────────────
#
# orcho-core persists ``meta.auto_detect`` ONLY for runs that started through
# the ``auto-detect`` selector channel: ``run_setup.py:147-155`` reads the
# scoped ``ORCHO_AUTODETECT_DECISION`` env (the serialized
# ``AutoDetectResolution`` — ``auto_detect.resolution_to_payload``) and persists
# it as the additive ``meta.auto_detect`` block; a manual concrete profile never
# writes the channel (confirmed in the T0/F2 audit + ``test_auto_detect_evidence``
# invariant). The block's *presence* is therefore equivalent to "this run was
# requested via the auto-detect selector", so ``requested_selector`` is a
# request fact (= the core selector token) set whenever the block exists — NOT a
# detector decision. This projection mirrors the persisted payload fields and
# adds a deterministic, agent-safe ``next_action``: MCP never re-implements the
# detector's classification, it only projects the recorded facts.
#
# The selector token is imported defensively from core (single source of truth)
# with a literal fallback so a stale core that predates the constant still loads.
try:
    from pipeline.project.auto_detect import (
        AUTO_DETECT_PROFILE_TOKEN as _AUTO_DETECT_PROFILE_TOKEN,
    )
except ImportError:  # pragma: no cover - exercised by the stale-core unit test
    _AUTO_DETECT_PROFILE_TOKEN = "auto-detect"

_DETECTION_STATE_RECOMMENDED = "recommended"
# Dispositions that are NOT trusted: the detector did not cleanly recommend, so
# the run needs an explicit operator profile choice before a confident re-run.
_FALLBACK_DETECTION_STATES = frozenset({
    "low_confidence_fallback",
    "detector_error_fallback",
    "failed",
})
_KNOWN_DETECTION_STATES = frozenset(
    {_DETECTION_STATE_RECOMMENDED} | _FALLBACK_DETECTION_STATES
)


def _coerce_optional_float(raw: object) -> float | None:
    """Coerce a payload value to a ``float``, rejecting bool and non-numbers.

    ``bool`` is an ``int`` subclass in Python; a stray ``True`` confidence
    would otherwise read as ``1.0`` and silently fake a perfect-confidence
    recommendation, so it is rejected explicitly.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def _coerce_bool_flag(raw: object) -> bool:
    """Coerce a payload value to ``True`` only when it is exactly ``True``.

    Defensive: a missing / truthy-but-non-bool value reads as ``False`` so a
    malformed payload never fabricates a fallback-used signal.
    """
    return raw is True


def _coerce_str_list(raw: object) -> list[str]:
    """Coerce a payload value to a clean ``list[str]`` (order preserved).

    Anything not a list returns ``[]``; members are str-coerced and empty
    strings dropped, so a malformed ``risk_flags`` never raises.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        s = str(item) if item is not None else ""
        if s:
            out.append(s)
    return out


@dataclass(frozen=True)
class AutoDetectProjection:
    """Normalised ``meta.auto_detect`` read-model — presentation-free.

    Every field is coerced to a safe shape. ``requested_selector`` is the
    core selector token, set whenever the block is present (the presence
    invariant). ``disposition`` is the normalised trust label derived from
    ``detection_state``; ``trusted`` is ``True`` only for a clean
    ``recommended`` resolution. ``next_action`` is the deterministic
    agent-control contract (see :func:`_auto_detect_next_action`): ``None``
    when trusted, else an ``operator_input_required`` pointer that never
    carries an empty ``args.profile``.
    """

    requested_selector: str | None = None
    detection_state: str | None = None
    selected_profile: str | None = None
    selected_mode: str | None = None
    recommended_profile: str | None = None
    recommended_mode: str | None = None
    policy: str | None = None
    confidence: float | None = None
    fallback_used: bool = False
    confirmation_state: str | None = None
    risk_flags: list[str] = field(default_factory=list)
    rationale: str | None = None
    error_reason: str | None = None
    fallback_reason: str | None = None
    disposition: str | None = None
    trusted: bool = False
    next_action: NextActionRecord | None = None
    recommended_topology: str | None = None
    delivery_scope: str | None = None
    projects: list[str] = field(default_factory=list)
    topology_reason: str | None = None
    topology_next_actions: list[NextActionRecord] = field(default_factory=list)


# Closed topology vocabulary mirrored from core ``run_shape.RunTopology``. Only
# ``cross_recommended`` surfaces the three typed choices; an unknown / missing
# value degrades to no topology variants (defensive, version-skew safe).
_TOPOLOGY_CROSS_RECOMMENDED = "cross_recommended"


def _topology_next_actions(
    recommended_topology: str | None,
    selected_profile: str | None,
    projects: list[str],
) -> list[NextActionRecord]:
    """The three typed topology choices for a ``cross_recommended`` topology.

    Mirrors the core CLI's three operator choices (T3) as advisory,
    ``operator_input_required`` records — MCP never starts a cross run or widens
    delivery itself. Returns ``[]`` unless the topology is ``cross_recommended``
    with non-empty ``projects``.

    Agent-control invariant (same as the fallback ``next_action``): a record
    NEVER carries an empty ``args.profile``. The cross choice omits the
    ``profile`` key entirely (the operator chooses the cross scope); the two
    mono choices carry ``args.profile`` only when a non-empty selected profile
    is known (re-run the same mono profile under the chosen delivery scope).

    Machine-readable selector invariant (F1): every record carries a stable
    ``args.topology_choice`` (``start_cross`` / ``expanded_mono`` /
    ``strict_mono``) so a client can distinguish the three variants WITHOUT
    parsing ``intent`` prose. The two mono choices additionally carry the
    resulting ``args.delivery_scope`` (``expanded_mono`` / ``strict_mono``);
    the cross choice's scope is the cross run itself, so it advertises the
    ``delivery_scope`` (``cross``) in its ``input_schema`` rather than as a
    pre-filled arg the operator has not yet confirmed.
    """
    if recommended_topology != _TOPOLOGY_CROSS_RECOMMENDED or not projects:
        return []
    project_list = ", ".join(projects)
    mono_args: dict[str, object] = {}
    if selected_profile:
        mono_args = {"profile": selected_profile}
    return [
        NextActionRecord(
            intent=(
                f"Start a cross-project run spanning {project_list} "
                "(delivery scope: cross)."
            ),
            tool="orcho_run_start",
            args={"topology_choice": "start_cross"},
            optional=True,
            kind="operator_input_required",
            requires_operator_input=True,
            input_schema={
                "type": "object",
                "required": ["projects"],
                "properties": {
                    "projects": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Project aliases to span in the cross-project "
                            f"run: {project_list}."
                        ),
                    },
                    "delivery_scope": {
                        "const": "cross",
                        "description": (
                            "Resulting delivery scope once the cross-project "
                            "run starts."
                        ),
                    },
                },
            },
        ),
        NextActionRecord(
            intent=(
                "Continue this mono run and allow expanded delivery — sibling "
                f"changes across {project_list} are disclosed, not blocked "
                "(delivery scope: expanded_mono)."
            ),
            tool="orcho_run_start",
            args={
                **mono_args,
                "topology_choice": "expanded_mono",
                "delivery_scope": "expanded_mono",
            },
            optional=True,
            kind="operator_input_required",
            requires_operator_input=True,
        ),
        NextActionRecord(
            intent=(
                "Continue strict mono — sibling-repo changes are a reversible "
                "delivery-scope violation (delivery scope: strict_mono)."
            ),
            tool="orcho_run_start",
            args={
                **mono_args,
                "topology_choice": "strict_mono",
                "delivery_scope": "strict_mono",
            },
            optional=True,
            kind="operator_input_required",
            requires_operator_input=True,
        ),
    ]


def _auto_detect_next_action(
    detection_state: str | None,
    selected_profile: str | None,
    recommended_profile: str | None,
) -> NextActionRecord | None:
    """Deterministic next-action for an auto-detect disposition.

    Contract (agent-control invariant — MCP NEVER emits ``args.profile`` as
    ``None`` / empty):

    - ``recommended`` (trusted) or an unknown/missing state → ``None``: trust
      the selected profile, no operator step needed.
    - a fallback disposition (``low_confidence_fallback`` /
      ``detector_error_fallback`` / ``failed``):
        * ``candidate_profile`` = first NON-empty of
          (``selected_profile``, ``recommended_profile``). When present, an
          ``operator_input_required`` re-run pointer carrying a NON-empty
          ``args.profile`` (the task still needs operator confirmation, so it
          is never a ``ready_call``).
        * Otherwise an ``operator_input_required`` pointer with NO ``profile``
          key, carrying a machine-readable ``input_schema`` that names the
          missing ``profile`` parameter (so the agent need not parse
          ``intent`` prose), asking the operator to choose a profile and
          re-run, or inspect the decision via ``orcho_run_status`` /
          ``orcho_run_evidence``.
    """
    if detection_state not in _FALLBACK_DETECTION_STATES:
        return None
    candidate_profile = selected_profile or recommended_profile
    if candidate_profile:
        return NextActionRecord(
            intent=(
                f"Auto-detect fell back ({detection_state}); re-run "
                f"orcho_run_start with the explicit profile "
                f"'{candidate_profile}' once an operator confirms it."
            ),
            tool="orcho_run_start",
            args={"profile": candidate_profile},
            optional=False,
            kind="operator_input_required",
            requires_operator_input=True,
        )
    return NextActionRecord(
        intent=(
            f"Auto-detect could not resolve a profile ({detection_state}); "
            "choose an explicit profile and re-run orcho_run_start, or inspect "
            "the decision via orcho_run_status / orcho_run_evidence before "
            "retrying."
        ),
        tool="orcho_run_start",
        args={},
        optional=False,
        kind="operator_input_required",
        requires_operator_input=True,
        # No concrete candidate exists, so the final ``profile`` argument is
        # intentionally omitted from ``args``. Describe the missing input
        # machine-readably (the ``operator_input_required`` contract requires
        # ``choices`` and/or ``input_schema`` whenever final args are dropped)
        # so the agent can collect it without reading terminal prose. The
        # executable profile names are not cheaply available here without an
        # IO read, so point at ``orcho_profiles_list`` for the live catalogue
        # instead of inlining a (possibly stale) ``choices`` enumeration.
        input_schema={
            "type": "object",
            "required": ["profile"],
            "properties": {
                "profile": {
                    "type": "string",
                    "description": (
                        "Explicit executable profile name to re-run with. "
                        "Enumerate the available executable profiles via "
                        "orcho_profiles_list (the ``profiles`` field)."
                    ),
                },
            },
        },
    )


def project_auto_detect(meta: object) -> AutoDetectProjection | None:
    """Project ``meta.auto_detect`` into the normalised read-model.

    Pure transform of the already-loaded ``meta`` dict — no IO, no SDK call.
    Returns ``None`` when ``meta`` is not a dict or ``meta['auto_detect']`` is
    not a dict (the run did not start through the ``auto-detect`` selector).
    Otherwise every core field is defensively coerced (a partial / junk
    payload never raises), ``requested_selector`` is set to the core selector
    token (presence invariant), ``disposition`` / ``trusted`` are derived from
    ``detection_state``, and a deterministic ``next_action`` is attached.
    """
    block = meta.get("auto_detect") if isinstance(meta, dict) else None
    if not isinstance(block, dict):
        return None

    # Normalise ``detection_state`` to a KNOWN value or ``None``. An
    # unrecognised / future state degrades to ``None`` (no disposition, no
    # next_action) so the wire ``Literal`` contract can never raise on a
    # version-skewed core — the defensive equivalent of the worktree
    # ``unknown_continuity`` fallback.
    raw_detection_state = _coerce_optional_str(block.get("detection_state"))
    detection_state = (
        raw_detection_state
        if raw_detection_state in _KNOWN_DETECTION_STATES else None
    )
    selected_profile = _coerce_optional_str(block.get("actual_profile"))
    selected_mode = _coerce_optional_str(block.get("actual_mode"))
    recommended_profile = _coerce_optional_str(block.get("recommended_profile"))
    recommended_mode = _coerce_optional_str(block.get("recommended_mode"))
    policy = _coerce_optional_str(block.get("policy"))
    confidence = _coerce_optional_float(block.get("confidence"))
    rationale = _coerce_optional_str(block.get("rationale"))
    risk_flags = _coerce_str_list(block.get("risk_flags"))
    fallback_used = _coerce_bool_flag(block.get("fallback_used"))
    confirmation_state = _coerce_optional_str(block.get("confirmation_state"))
    error_reason = _coerce_optional_str(block.get("error_reason"))
    fallback_reason = _coerce_optional_str(block.get("fallback_reason"))
    # Topology axis (T2): independent of the profile recommendation. All
    # defensively coerced so a partial / version-skewed payload never raises;
    # a stale core that predates the topology fields simply yields None / [].
    recommended_topology = _coerce_optional_str(block.get("recommended_topology"))
    delivery_scope = _coerce_optional_str(block.get("delivery_scope"))
    projects = _coerce_str_list(block.get("delivery_projects"))
    topology_reason = _coerce_optional_str(block.get("topology_reason"))

    disposition = (
        detection_state if detection_state in _KNOWN_DETECTION_STATES else None
    )
    trusted = detection_state == _DETECTION_STATE_RECOMMENDED
    next_action = _auto_detect_next_action(
        detection_state, selected_profile, recommended_profile,
    )
    topology_next_actions = _topology_next_actions(
        recommended_topology, selected_profile, projects,
    )

    return AutoDetectProjection(
        requested_selector=_AUTO_DETECT_PROFILE_TOKEN,
        detection_state=detection_state,
        selected_profile=selected_profile,
        selected_mode=selected_mode,
        recommended_profile=recommended_profile,
        recommended_mode=recommended_mode,
        policy=policy,
        confidence=confidence,
        fallback_used=fallback_used,
        confirmation_state=confirmation_state,
        risk_flags=risk_flags,
        rationale=rationale,
        error_reason=error_reason,
        fallback_reason=fallback_reason,
        disposition=disposition,
        trusted=trusted,
        next_action=next_action,
        recommended_topology=recommended_topology,
        delivery_scope=delivery_scope,
        projects=projects,
        topology_reason=topology_reason,
        topology_next_actions=topology_next_actions,
    )


# ── Provider-pressure projection ────────────────────────────────────────────
#
# The single read-model owner for core-typed *provider pressure*. The ONLY
# source of truth is the core-typed errors/halt slice
# (``sdk.get_errors_halt`` → ``ErrorsAndHalt.provider_runtime`` /
# ``ErrorsAndHalt.recovery``); MCP never derives the condition by parsing raw
# provider output, logs, or events. Two mutually-exclusive branches, never
# merged:
#
# - ``provider_runtime`` (``ProviderRuntimeFailure``) — a runtime fault /
#   rate-limit. ``recommended_action`` comes from core (``resume_or_retry_phase``)
#   and the resume/retry next-actions apply.
# - ``provider_access`` (``ProviderAccessRecovery``) — a loss of provider access.
#   The projection stamps ``recommended_action='switch_runtime_or_restore_access'``
#   and carries the ``replacements`` candidates; its next-actions are the
#   distinct restore-access/switch-runtime path, never the resume_or_retry_phase
#   semantics of the runtime branch.
#
# Future fields (``pressure_kind`` / ``retry_state`` / ``reset_at`` /
# ``wait_hint``) are read DEFENSIVELY via ``getattr`` so today's SDK slice
# yields ``None`` while a future core (or a fixture stand-in) can populate them
# without an MCP change. MCP never fabricates a reset time.

_PROVIDER_ACCESS_RECOMMENDED_ACTION = "switch_runtime_or_restore_access"
_PROVIDER_PRESSURE_SOURCE_RUNTIME = "provider_runtime"
_PROVIDER_PRESSURE_SOURCE_ACCESS = "provider_access"
_PARKED_RETRY_STATE = "parked_until_reset"


@dataclass(frozen=True)
class ProviderPressureProjection:
    """Normalised core-typed provider-pressure state for one run.

    ``condition_present`` is ``False`` (with every other field at its empty
    default) when core attached no typed provider failure — a generic run
    stays generic. When ``True``, ``source`` is the mutually-exclusive branch
    (``provider_runtime`` / ``provider_access``) and the remaining fields carry
    the core-typed facts. ``run_id`` is retained so the shared next-actions
    helper can build forwardable calls without re-threading it.

    Future fields (``pressure_kind`` / ``retry_state`` / ``reset_at`` /
    ``wait_hint``) are ``None`` on today's SDK slice and only populated when
    core (or a fixture stand-in) carries the attribute.
    """

    run_id: str
    condition_present: bool
    source: str | None = None
    failure_kind: str | None = None
    recoverable: bool = False
    recommended_action: str | None = None
    phase: str | None = None
    runtime: str | None = None
    model: str | None = None
    sanitized_message: str | None = None
    pressure_kind: str | None = None
    retry_state: str | None = None
    reset_at: str | None = None
    wait_hint: str | None = None
    replacements: list[dict[str, str]] = field(default_factory=list)


def _future_field(obj: object, attr: str) -> str | None:
    """Read a future core attribute defensively as a non-empty str or ``None``.

    Today's SDK dataclasses do not carry these attributes, so ``getattr``
    returns ``None``; a future core build (or a fixture stand-in object)
    populates them and they flow through unchanged. MCP never fabricates the
    value.
    """
    val = getattr(obj, attr, None)
    if val is None:
        return None
    s = str(val)
    return s or None


def _project_provider_runtime(
    run_id: str, pr: object,
) -> ProviderPressureProjection:
    """Project a core ``ProviderRuntimeFailure`` into the read-model."""
    message = getattr(pr, "provider_message", "") or ""
    return ProviderPressureProjection(
        run_id=run_id,
        condition_present=True,
        source=_PROVIDER_PRESSURE_SOURCE_RUNTIME,
        failure_kind=_future_field(pr, "failure_kind"),
        recoverable=bool(getattr(pr, "recoverable", False)),
        recommended_action=_future_field(pr, "recommended_action"),
        phase=_future_field(pr, "failed_phase"),
        runtime=_future_field(pr, "runtime"),
        model=_future_field(pr, "model"),
        sanitized_message=message or None,
        pressure_kind=_future_field(pr, "pressure_kind"),
        retry_state=_future_field(pr, "retry_state"),
        reset_at=_future_field(pr, "reset_at"),
        wait_hint=_future_field(pr, "wait_hint"),
    )


def _project_provider_access(
    run_id: str, recovery: object,
) -> ProviderPressureProjection:
    """Project a core ``ProviderAccessRecovery`` into the read-model.

    A separate branch from the runtime failure: the recommended action is the
    fixed ``switch_runtime_or_restore_access`` and the ``replacements``
    candidates are carried verbatim (runtime/model pairs). The two branches'
    semantics are never merged.
    """
    replacements: list[dict[str, str]] = []
    for r in getattr(recovery, "replacements", ()) or ():
        rt = getattr(r, "runtime", None)
        md = getattr(r, "model", None)
        replacements.append({
            "runtime": str(rt) if rt else "",
            "model": str(md) if md else "",
        })
    return ProviderPressureProjection(
        run_id=run_id,
        condition_present=True,
        source=_PROVIDER_PRESSURE_SOURCE_ACCESS,
        failure_kind=_future_field(recovery, "failure_kind"),
        recoverable=bool(getattr(recovery, "recoverable", False)),
        recommended_action=_PROVIDER_ACCESS_RECOMMENDED_ACTION,
        phase=_future_field(recovery, "failed_phase"),
        runtime=_future_field(recovery, "runtime"),
        model=_future_field(recovery, "model"),
        sanitized_message=None,
        pressure_kind=_future_field(recovery, "pressure_kind"),
        retry_state=_future_field(recovery, "retry_state"),
        reset_at=_future_field(recovery, "reset_at"),
        wait_hint=_future_field(recovery, "wait_hint"),
        replacements=replacements,
    )


def project_provider_pressure_from_errors_halt(
    run_id: str, eh: object,
) -> ProviderPressureProjection:
    """Project provider pressure from an ALREADY-fetched errors/halt slice.

    The pure mapping half of :func:`project_provider_pressure`, split out so a
    caller that already holds the ``sdk.get_errors_halt`` result (e.g. the
    evidence ``errors`` slice) reuses the SAME single source without a second
    SDK read. ``provider_runtime`` takes priority over ``recovery`` (mutually
    exclusive in core, but the runtime fault is the more specific signal).
    Returns ``condition_present=False`` when neither is present.
    """
    pr = getattr(eh, "provider_runtime", None)
    if pr is not None:
        return _project_provider_runtime(run_id, pr)

    recovery = getattr(eh, "recovery", None)
    if recovery is not None:
        return _project_provider_access(run_id, recovery)

    return ProviderPressureProjection(run_id=run_id, condition_present=False)


def project_provider_pressure(run_id: str) -> ProviderPressureProjection:
    """Project the core-typed provider-pressure condition for a run.

    Reads ONLY the core-typed errors/halt slice via ``sdk.get_errors_halt``
    (wrapped in the shared SDK→MCP error owner), then delegates to
    :func:`project_provider_pressure_from_errors_halt` — the one mapping shared
    with the evidence slice. When neither ``provider_runtime`` nor ``recovery``
    is present the projection reports ``condition_present=False`` so a generic
    failure stays generic — never a fabricated provider-pressure condition.
    """
    with map_sdk_errors(run_id):
        eh = _sdk_get_errors_halt(run_id, cwd=None)
    return project_provider_pressure_from_errors_halt(run_id, eh)


def _pp_inspect_action(run_id: str) -> NextActionRecord:
    """Ready-to-forward errors-slice inspection of the provider pressure."""
    return NextActionRecord(
        intent="Inspect the provider-pressure errors/halt slice for this run.",
        tool="orcho_run_evidence",
        args={"run_id": run_id, "slice": "errors"},
        optional=True,
        kind="ready_call",
    )


def _pp_status_action(run_id: str) -> NextActionRecord:
    """Ready-to-forward status snapshot for the provider-pressure run."""
    return NextActionRecord(
        intent="Inspect the run's current status snapshot.",
        tool="orcho_run_status",
        args={"run_id": run_id},
        optional=True,
        kind="ready_call",
    )


def _pp_resume_action(
    run_id: str, *, intent: str, optional: bool,
    context: dict[str, str] | None = None,
) -> NextActionRecord:
    """Ready-to-forward ``orcho_run_resume`` for the provider-pressure run.

    Never carries operator feedback — provider pressure is resolved by
    resuming/retrying the interrupted phase (or waiting for a reset window),
    not by injecting a human-directed retry.
    """
    return NextActionRecord(
        intent=intent,
        tool="orcho_run_resume",
        args={"run_id": run_id},
        optional=optional,
        kind="ready_call",
        context=context or None,
    )


def build_provider_pressure_next_actions(
    projection: ProviderPressureProjection | None,
) -> list[NextActionRecord]:
    """The single source of provider-pressure ``next_actions``.

    Shared by diagnose, status (``run_reads``), summary, evidence and
    live_status so the surfaces never drift. Returns ``[]`` for an absent
    condition. Otherwise, conservative and typed, never emitting
    retry_feedback / operator-feedback and never implying a passed
    review/delivery:

    - ``provider_access`` — inspect, then a distinct restore-access/switch-runtime
      resume path (never the runtime branch's retry-phase semantics);
    - parked (future ``retry_state='parked_until_reset'`` or ``reset_at``) —
      wait-until-reset, resume-after-reset, inspect;
    - recoverable ``provider_runtime`` — inspect, resume_or_retry_phase
      (deterministic, ``optional=False``), status;
    - exhausted-without-reset — inspect, resume_or_retry_phase (optional, no
      reset time invented).
    """
    if projection is None or not projection.condition_present:
        return []
    run_id = projection.run_id

    if projection.source == _PROVIDER_PRESSURE_SOURCE_ACCESS:
        return [
            _pp_inspect_action(run_id),
            _pp_resume_action(
                run_id,
                intent=(
                    "Restore provider access or switch the runtime, then "
                    "resume the run."
                ),
                optional=True,
            ),
            _pp_status_action(run_id),
        ]

    parked = (
        projection.retry_state == _PARKED_RETRY_STATE
        or bool(projection.reset_at)
    )
    if parked:
        ctx: dict[str, str] = {}
        if projection.reset_at:
            ctx["reset_at"] = projection.reset_at
        if projection.wait_hint:
            ctx["wait_hint"] = projection.wait_hint
        if projection.reset_at:
            wait_intent = (
                "Wait until the provider reset window "
                f"({projection.reset_at}) passes, then re-check status."
            )
        elif projection.wait_hint:
            wait_intent = (
                "Wait until the provider reset window passes "
                f"({projection.wait_hint}), then re-check status."
            )
        else:
            wait_intent = (
                "Wait until the provider reset window passes, then re-check "
                "status."
            )
        return [
            NextActionRecord(
                intent=wait_intent,
                tool="orcho_run_status",
                args={"run_id": run_id},
                optional=True,
                kind="ready_call",
                context=ctx or None,
            ),
            _pp_resume_action(
                run_id,
                intent="Resume the run after the provider reset window passes.",
                optional=True,
                context=ctx or None,
            ),
            _pp_inspect_action(run_id),
        ]

    if projection.recoverable:
        return [
            _pp_inspect_action(run_id),
            _pp_resume_action(
                run_id,
                intent=(
                    "Resume the run to retry the phase the provider "
                    "interrupted."
                ),
                optional=False,
            ),
            _pp_status_action(run_id),
        ]

    # Exhausted without a reset window — conservative inspect + retry, never
    # inventing a reset time.
    return [
        _pp_inspect_action(run_id),
        _pp_resume_action(
            run_id,
            intent="Retry the interrupted phase once provider capacity returns.",
            optional=True,
        ),
    ]


def build_provider_pressure(
    projection: ProviderPressureProjection | None,
) -> ProviderPressure | None:
    """Build the wire :class:`ProviderPressure` from a projection.

    The single constructor every surface (status / evidence / diagnose /
    summary / live_status) uses, so the wire model and its ``next_actions``
    are identical everywhere. Returns ``None`` for an absent condition (a
    generic failure surfaces ``provider_pressure == None``).
    """
    if projection is None or not projection.condition_present:
        return None
    return ProviderPressure(
        failure_kind=projection.failure_kind,
        recoverable=projection.recoverable,
        phase=projection.phase,
        pressure_kind=projection.pressure_kind,
        retry_state=projection.retry_state,
        reset_at=projection.reset_at,
        wait_hint=projection.wait_hint,
        sanitized_message=projection.sanitized_message,
        recommended_action=projection.recommended_action,
        next_actions=build_provider_pressure_next_actions(projection),
    )


__all__ = [
    "AutoDetectProjection",
    "FollowupLineageProjection",
    "HandoffReadModel",
    "PendingHandoffProjection",
    "ProviderPressureProjection",
    "ProviderSessionFallbackProjection",
    "RetryStateProjection",
    "RunDiagnosisProjection",
    "TerminalConsistencyProjection",
    "WorktreeContinuityProjection",
    "build_provider_pressure",
    "build_provider_pressure_next_actions",
    "is_provider_session_fallback_event",
    "merged_halt_reason_from_meta",
    "merged_status_from_meta",
    "project_auto_detect",
    "project_followup_lineage",
    "project_handoff_read_model",
    "project_pending_handoff",
    "project_provider_pressure",
    "project_provider_pressure_from_errors_halt",
    "project_provider_session_fallback",
    "project_retry_state",
    "project_run_diagnosis",
    "project_terminal_consistency",
    "project_worktree_continuity",
]
