"""orcho_mcp.schemas.read — wire models for the read tool family.

Covers history, status, metrics, event-tail, skills catalogue, and
profiles catalogue. These tools speak SDK (``services/`` is their
implementation home) and the wire shapes here mirror what those
services return.

``RunStatus`` reaches into ``shared.NextActionRecord`` for suggested
follow-ups; everything else is self-contained.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from orcho_mcp.schemas.observe import CurrentSubtaskRecord
from orcho_mcp.schemas.shared import (
    ContinuationSubjectLiteral,
    NextActionRecord,
    ProviderPressure,
    RecommendedNextActionLiteral,
    RecoveryLineage,
)

# ── orcho_run_history ────────────────────────────────────────────────────────


class RunRecord(BaseModel):
    """Compact summary of a single run for history listings."""
    run_id: str
    project: str
    task: str
    status: str
    timestamp: str
    total_tokens: int = 0
    total_duration_s: float = 0.0
    rounds: int = 0


class HistoryResult(BaseModel):
    runs: list[RunRecord]


# ── orcho_run_status ─────────────────────────────────────────────────────────


class ArtefactRefRecord(BaseModel):
    """Wire model for one readable artefact of a run.

    Mirrors ``sdk.ArtefactRef`` with one deliberate tightening:
    ``kind`` is a closed ``Literal`` on the wire side so clients can
    branch on a known enum, while the SDK keeps ``kind: str`` for
    forward compatibility. Adding a future kind is a paired core+MCP
    change.

    ``size_bytes`` is ``None`` for composable resources — today only
    ``evidence``, which ``sdk.evidence.collect_evidence`` assembles
    from multiple files at read time, so no single ``Content-Length``
    is meaningful.
    """

    kind: Literal["parsed_plan", "evidence", "diff"]
    uri: str = Field(
        description="MCP resource URI for fetching the artefact, e.g. "
                    "``orcho://runs/<id>/parsed_plan.json``.",
    )
    mime: str = Field(
        description="MIME type of the artefact content.",
    )
    size_bytes: int | None = Field(
        default=None,
        description="Physical artefact size from ``os.stat``. ``None`` for "
                    "composable resources assembled at read time (evidence).",
    )


class FollowupLineage(BaseModel):
    """Follow-up lineage around a run, surfaced on ``orcho_run_status``.

    ``parent_*`` describe the run this one continues (when it is itself a
    ``resume_mode="followup"`` child). The ``active_child_*`` fields plus
    the recommendation describe the newest *unfinished* follow-up child of
    this run: when one exists, ``recommended_action="resume_child"`` and
    ``recommended_run_id`` names the child to resume — the structured
    equivalent of the CLI's "Resume active follow-up <child>" guidance, so
    an operator does not resume a parent that a live child has moved past.

    All fields are ``None`` / ``False`` when there is no lineage to report
    (a fresh single-project run with no parent and no follow-up child).
    """

    run_id: str = Field(description="The inspected run.")
    parent_run_id: str | None = Field(
        default=None,
        description="Parent run this one continues when it is a follow-up "
                    "child (``meta.parent_run_id``); ``None`` otherwise.",
    )
    parent_status: str | None = Field(
        default=None,
        description="Merged status of ``parent_run_id`` when resolvable.",
    )
    resume_mode: str | None = Field(
        default=None,
        description="This run's ``meta.resume_mode`` (``followup`` / "
                    "``checkpoint`` / ``None`` for a fresh run).",
    )
    has_active_child_followup: bool = Field(
        default=False,
        description="True when a newer, still-unfinished follow-up child of "
                    "this run exists — resuming this run is then discouraged "
                    "in favour of the child.",
    )
    active_child_run_id: str | None = Field(
        default=None,
        description="The newest unfinished follow-up child's run id, when "
                    "one exists.",
    )
    active_child_status: str | None = Field(
        default=None,
        description="Status of the active follow-up child.",
    )
    active_child_handoff_id: str | None = Field(
        default=None,
        description="Active ``meta.phase_handoff.id`` of the child when it "
                    "is itself paused awaiting a decision; ``None``.",
    )
    recommended_action: Literal["resume_child"] | None = Field(
        default=None,
        description="``resume_child`` when an active follow-up child should "
                    "be resumed instead of this run; ``None`` otherwise.",
    )
    recommended_run_id: str | None = Field(
        default=None,
        description="The run id to resume per ``recommended_action`` (the "
                    "active child); ``None`` when no recommendation.",
    )
    recommendation: str | None = Field(
        default=None,
        description="Human-readable recommendation mirroring the CLI's "
                    "'Resume active follow-up <child>' guidance; ``None`` "
                    "when no active child follow-up exists.",
    )


class WorktreeContinuity(BaseModel):
    """Worktree-continuity mode for a run, surfaced on ``orcho_run_status``.

    Projects the persisted ``meta['worktree']`` block (and its
    ``followup_continuity`` sub-block) so a client sees how the run picked
    up its worktree without parsing logs. ``subject_mode`` is the
    normalised classification:

    - ``same_run_retained`` — not a follow-up; the run kept its own
      worktree (a fresh / checkpoint / provider-fallback run — the
      same-run worktree is preserved);
    - ``reused_parent`` — a follow-up that reused the parent's dirty
      worktree, carrying the parent's uncommitted diff into the child
      (``diff_source='worktree'``);
    - ``clean_head_no_undelivered_diff`` — a follow-up that started fresh
      from HEAD because the parent had no undelivered diff
      (``diff_source='none'``);
    - ``blocked_parent_diff_unavailable`` — a follow-up blocked before any
      write phase because the parent's undelivered diff exists only as a
      ``diff.patch`` artifact this run will not replay
      (``diff_source='artifact'``); ``block_message`` carries the core
      warning that starting on a clean HEAD would silently drop the
      change and that resuming the parent recovers it;
    - ``unknown_continuity`` — a follow-up block with an unrecognised
      ``diff_source`` (defensive).
    """

    has_worktree: bool = Field(
        description="True when the run has a persisted ``meta['worktree']`` "
                    "block; False when no worktree state was recorded.",
    )
    subject_mode: Literal[
        "same_run_retained",
        "reused_parent",
        "clean_head_no_undelivered_diff",
        "blocked_parent_diff_unavailable",
        "unknown_continuity",
    ] | None = Field(
        default=None,
        description="Normalised worktree-continuity mode; ``None`` when no "
                    "worktree block exists.",
    )
    isolation: str | None = Field(
        default=None,
        description="Worktree isolation mode (``off`` / ``per_run`` / …).",
    )
    path: str | None = Field(
        default=None,
        description="Worktree checkout path on disk.",
    )
    diff_source: Literal["worktree", "artifact", "none"] | None = Field(
        default=None,
        description="Raw follow-up diff source from core: ``worktree`` "
                    "(parent's dirty worktree reused), ``artifact`` "
                    "(diff.patch only — blocked), ``none`` (no undelivered "
                    "diff). ``None`` for a non-follow-up run.",
    )
    blocked: bool = Field(
        default=False,
        description="True when the follow-up was blocked before any write "
                    "phase (parent diff/worktree unavailable).",
    )
    block_message: str | None = Field(
        default=None,
        description="Core warning explaining the block — that starting on a "
                    "clean HEAD would silently drop the parent's change and "
                    "that resuming the parent recovers it. ``None`` when not "
                    "blocked. Surfaced verbatim so clients show it without "
                    "parsing logs.",
    )
    mode_label: str | None = Field(
        default=None,
        description="Core's verbatim follow-up mode label "
                    "(e.g. ``reused parent <path>`` / ``clean HEAD (parent "
                    "had no undelivered diff)``). ``None`` for a "
                    "non-follow-up run.",
    )
    worktree_preserved: bool = Field(
        default=False,
        description="True when a usable worktree exists (every mode except "
                    "``blocked``) — e.g. a same-run provider fallback keeps "
                    "the worktree.",
    )
    degraded_reason: str | None = Field(
        default=None,
        description="Non-fatal isolation degrade reason from core, if any.",
    )
    is_followup_continuity: bool = Field(
        default=False,
        description="True when a ``followup_continuity`` sub-block was "
                    "present (the run is a follow-up).",
    )


class AutoDetectProjection(BaseModel):
    """Typed projection of ``meta.auto_detect`` for ``orcho_run_status``.

    orcho-core persists ``meta.auto_detect`` ONLY for runs started through
    the ``auto-detect`` selector channel (a manual concrete profile never
    writes it), so the block's *presence* is equivalent to "this run was
    requested via the auto-detect selector". ``requested_selector`` records
    that request fact (the core ``auto-detect`` token) — it is NOT a detector
    decision and is always populated whenever this projection exists.

    The detector's own outcome is ``detection_state`` /
    ``selected_profile`` + ``selected_mode`` (the profile + mode the run
    actually started with) / ``recommended_profile`` + ``recommended_mode``
    (what the detector proposed). ``disposition`` is the normalised
    trust label and ``trusted`` is ``True`` only for a clean
    ``recommended`` resolution.

    ``next_action`` is a deterministic agent-control contract. For a
    trusted (``recommended``) resolution it is ``None`` — trust the
    selected profile. For a fallback resolution
    (``low_confidence_fallback`` / ``detector_error_fallback`` /
    ``failed``) it is always an ``operator_input_required`` pointer at
    ``orcho_run_start``: when a concrete profile is available it carries a
    NON-empty ``args.profile`` to re-run with; when no profile could be
    resolved it carries NO ``profile`` key and asks the operator to choose
    one (or inspect the decision). It NEVER emits ``args.profile`` as
    ``None`` / empty.
    """

    requested_selector: str | None = Field(
        default=None,
        description="Run-start selector token the run was requested through "
                    "(the core ``auto-detect`` token). Present exactly when "
                    "this projection exists — a request fact derived from the "
                    "``meta.auto_detect`` presence invariant, not a detector "
                    "decision.",
    )
    detection_state: Literal[
        "recommended",
        "low_confidence_fallback",
        "detector_error_fallback",
        "failed",
    ] | None = Field(
        default=None,
        description="Raw detector outcome state from core. ``None`` when the "
                    "persisted value is missing/unrecognised.",
    )
    selected_profile: str | None = Field(
        default=None,
        description="Profile the run actually started with "
                    "(core ``actual_profile``).",
    )
    selected_mode: str | None = Field(
        default=None,
        description="Operating mode the run actually started with "
                    "(core ``actual_mode``).",
    )
    recommended_profile: str | None = Field(
        default=None,
        description="Profile the detector recommended (``None`` on a detector "
                    "error / failed resolution).",
    )
    recommended_mode: str | None = Field(
        default=None,
        description="Operating mode the detector recommended (``None`` on a "
                    "detector error / failed resolution).",
    )
    policy: str | None = Field(
        default=None,
        description="Auto-detect policy that governed the resolution "
                    "(e.g. ``confirm`` / ``trust_above_threshold``).",
    )
    confidence: float | None = Field(
        default=None,
        description="Detector confidence for the recommendation, when known.",
    )
    fallback_used: bool = Field(
        default=False,
        description="True when the resolution fell back to a default profile "
                    "instead of the detector's recommendation.",
    )
    confirmation_state: str | None = Field(
        default=None,
        description="How the recommendation was confirmed "
                    "(e.g. ``auto`` / ``accepted`` / ``override``), if any.",
    )
    risk_flags: list[str] = Field(
        default_factory=list,
        description="Risk flags the detector attached to the recommendation.",
    )
    rationale: str | None = Field(
        default=None,
        description="Detector rationale for the recommendation, if recorded.",
    )
    error_reason: str | None = Field(
        default=None,
        description="Why the detector errored, on an error / failed "
                    "resolution.",
    )
    fallback_reason: str | None = Field(
        default=None,
        description="Why a fallback profile was used, when applicable.",
    )
    disposition: Literal[
        "recommended",
        "low_confidence_fallback",
        "detector_error_fallback",
        "failed",
    ] | None = Field(
        default=None,
        description="Normalised trust disposition derived from "
                    "``detection_state``. ``None`` for an "
                    "unknown/missing state.",
    )
    trusted: bool = Field(
        default=False,
        description="True only for a clean ``recommended`` resolution — the "
                    "selected profile can be trusted without operator input.",
    )
    next_action: NextActionRecord | None = Field(
        default=None,
        description="Deterministic next step. ``None`` when trusted. For a "
                    "fallback disposition, an ``operator_input_required`` "
                    "pointer at ``orcho_run_start``: with a NON-empty "
                    "``args.profile`` when a concrete profile is available, "
                    "else with NO ``profile`` key (operator must choose / "
                    "inspect). Never carries an empty ``args.profile``.",
    )
    recommended_topology: str | None = Field(
        default=None,
        description="Recommended run topology (core ``recommended_topology``): "
                    "``mono`` or ``cross_recommended``. A ``cross_recommended`` "
                    "value is a recommendation only — it never changed the "
                    "selected profile or started a cross run.",
    )
    delivery_scope: str | None = Field(
        default=None,
        description="Delivery scope the run resolved under (core "
                    "``delivery_scope``): ``strict_mono`` / ``expanded_mono`` / "
                    "``cross``. A trusted / non-interactive resolution keeps "
                    "``strict_mono`` regardless of the recommended topology.",
    )
    projects: list[str] = Field(
        default_factory=list,
        description="Project aliases the topology recommendation implicates "
                    "(core ``delivery_projects``), primary alias first. Empty "
                    "for a mono recommendation.",
    )
    topology_reason: str | None = Field(
        default=None,
        description="Short, provider-neutral rationale for the recommended "
                    "topology (core ``topology_reason``).",
    )
    topology_next_actions: list[NextActionRecord] = Field(
        default_factory=list,
        description="The three typed topology choices surfaced only for a "
                    "``cross_recommended`` topology: (1) start a cross-project "
                    "run over ``projects``, (2) continue mono with expanded "
                    "delivery disclosing sibling changes, (3) continue strict "
                    "mono. Each is an ``operator_input_required`` record; none "
                    "carries an empty ``args.profile`` (the invariant holds — "
                    "the key is absent or a non-empty profile). Empty for a "
                    "mono recommendation.",
    )


class RecoveryRecommendation(BaseModel):
    """Lineage-aware recovery recommendation, surfaced on ``orcho_run_status``.

    Projected from the SAME ``services.run_lineage`` resolver that backs
    ``orcho_run_diagnose`` so a captain gets the typed continuation subject
    without a separate diagnose call, and the two surfaces never drift. For a
    given run the ``continuation_subject`` + ``recommended_next_action`` +
    ``recommended_run_id`` here equal those on ``orcho_run_diagnose``.

    ``None`` on ``orcho_run_status`` when the run has no non-trivial
    recommendation (e.g. an ordinary running run with no active child and no
    terminality). The durable facts behind the recommendation ride in the
    reused :class:`RecoveryLineage` submodel.

    ``from_run_plan`` is the right primitive ONLY for
    ``recommended_next_action='plan_artifact_continuation'`` (implement a
    persisted plan artifact as a new run) — never for finishing a retained diff
    or checkpoint, which is what ``resume_source_run`` /
    ``resume_active_child`` cover.
    """

    continuation_subject: ContinuationSubjectLiteral = Field(
        description="The typed durable subject this run's continuation should "
                    "act on.",
    )
    recommended_next_action: RecommendedNextActionLiteral = Field(
        description="The typed next action a captain should take.",
    )
    recommended_run_id: str | None = Field(
        default=None,
        description="The source / active-child / plan-owning run to act on; "
                    "``None`` for a ``stop_unknown`` dead-end.",
    )
    reason: str = Field(
        default="",
        description="One-line factual reason assembled from persisted lineage "
                    "facts; never parsed from log prose.",
    )
    lineage: RecoveryLineage = Field(
        default_factory=RecoveryLineage,
        description="Durable recovery-lineage facts (source pointer + "
                    "resumability, active child, plan-subject availability, "
                    "and the missing durable facts for a dead-end). Reused "
                    "verbatim from ``orcho_run_diagnose``.",
    )


class RunStatus(BaseModel):
    """Summary state snapshot for a single run.

    ``meta`` is the default summary projection of ``meta.json`` (phase bodies
    elided unless the caller opts back in via ``include``), while ``metrics`` is
    surfaced as a plain dict. The runtime shape of meta.json/metrics.json
    evolves and over-tightening here would force schema changes for every
    observability tweak.

    Delivery disposition is deliberately NOT projected as a dedicated typed axis
    here. The interactive delivery surfaces already own it without a second meta
    read: ``orcho_run_live_status`` carries the typed terminal disposition
    (``RunLiveTerminal.delivery_committed`` / ``delivery_published`` /
    ``delivery_pr_url``), ``orcho_delivery_gate`` projects the terminal
    ``delivery_completed`` kind with ``published`` / ``pr_url`` /
    ``delivery_notices``, and ``orcho_run_evidence`` (slice ``delivery``) carries
    the read-only ``DeliverySummaryRecord``. A caller that needs the raw facts
    from a status snapshot still reads them off the untyped ``meta`` dict
    (``meta['commit_delivery']``). Adding a fourth, parallel delivery axis to
    ``run_status`` would duplicate the source and risk drift, so it is left out
    unless a status-parity requirement is explicitly raised (aligned with the
    T1 gate kind and the T3 live-status disposition).
    """
    run_id: str
    run_dir: str
    meta: dict[str, Any]
    metrics: dict[str, Any] | None = None
    sub_runs: list[str] = Field(
        default_factory=list,
        description="Child run aliases for cross-project runs (empty for single-project).",
    )
    lineage: FollowupLineage | None = Field(
        default=None,
        description="Follow-up lineage around this run — parent linkage and, "
                    "when present, a recommendation to resume an active "
                    "follow-up child instead of this run. ``None`` when "
                    "lineage could not be projected.",
    )
    worktree_continuity: WorktreeContinuity | None = Field(
        default=None,
        description="Worktree-continuity mode (same-run retained / reused "
                    "parent / clean HEAD / blocked) projected from "
                    "``meta['worktree']``, including the core clean-HEAD "
                    "recovery warning. ``None`` when not projected.",
    )
    auto_detect: AutoDetectProjection | None = Field(
        default=None,
        description="Typed projection of the persisted ``meta.auto_detect`` "
                    "decision (requested selector + detector outcome + a "
                    "deterministic next_action). ``None`` for a run that did "
                    "not start through the ``auto-detect`` selector.",
    )
    recovery_recommendation: RecoveryRecommendation | None = Field(
        default=None,
        description="Lineage-aware recovery recommendation projected from the "
                    "same ``services.run_lineage`` resolver as "
                    "``orcho_run_diagnose`` — the typed continuation subject + "
                    "next action (resume the source checkpoint, resume the "
                    "active child, continue a plan artifact, or stop with the "
                    "missing facts) so a captain need not call diagnose "
                    "separately. ``None`` for an ordinary run with no "
                    "non-trivial recommendation (no terminality, no active "
                    "child). Additive — does NOT overload ``lineage``.",
    )
    next_actions: list[NextActionRecord] = Field(
        default_factory=list,
        description=(
            "Suggested follow-up tool calls derived from the run's "
            "current state (MCP UX A1, Principle 1). Empty when the "
            "run is mid-flight or terminal-success."
        ),
    )
    artefacts: list[ArtefactRefRecord] = Field(
        default_factory=list,
        description=(
            "Readable artefacts available for this run, with MCP "
            "resource URIs. Agent reads this to discover what it can "
            "fetch without scanning the run directory or remembering "
            "filename conventions."
        ),
    )
    provider_pressure: ProviderPressure | None = Field(
        default=None,
        description=(
            "Core-typed provider runtime/access failure (rate-limit / "
            "transient runtime fault / access loss) projected from the same "
            "``project_provider_pressure`` source as ``orcho_run_diagnose`` "
            "and ``orcho_run_evidence`` — so all surfaces report the same "
            "condition with the same conservative ``next_actions``. ``None`` "
            "for a generic failure with no core-typed provider source. The "
            "provider-pressure follow-ups live here in "
            "``provider_pressure.next_actions``; the legacy SDK-derived "
            "``next_actions`` field is unaffected."
        ),
    )
    current_subtask: CurrentSubtaskRecord | None = Field(
        default=None,
        description=(
            "Live progress coordinate for the in-flight ``subtask_dag`` "
            "subtask (index / total / goal / state), derived from the SAME "
            "observe walk (``build_latest_run_events_summary``) that backs "
            "``orcho_run_live_status`` — so status and live_status report the "
            "same subtask position for a run. ``None`` when no subtask is "
            "currently in flight (terminal run, or a phase with no active "
            "subtask); the absence is not an error. For continuous live "
            "progress prefer ``orcho_run_live_status``."
        ),
    )


# ── orcho_run_metrics ────────────────────────────────────────────────────────


class RunMetrics(BaseModel):
    run_id: str
    metrics: dict[str, Any]


class PhaseCost(BaseModel):
    """Typed per-phase economics row projected from ``metrics.json``.

    Mirrors one entry of the runtime ``metrics.json['phases']`` rollup
    (cumulative across retries). ``attempts`` is the count of attempts the
    phase ran; a phase that ran cleanly once carries ``attempts=1``, so the
    surplus ``attempts - 1`` is what feeds :attr:`RunEconomics.retry_rate`.
    """

    phase: str = Field(description="Phase name (the metrics phases-dict key).")
    total_tokens: int = Field(
        default=0,
        description="Cumulative tokens charged to this phase across attempts.",
    )
    duration_s: float = Field(
        default=0.0,
        description="Cumulative wall-clock seconds spent in this phase.",
    )
    attempts: int = Field(
        default=1,
        description="Number of attempts this phase ran (1 = no retry).",
    )


class RunEconomics(BaseModel):
    """Typed run-economics projection — a thin typed view over metrics.json.

    Derived entirely from the raw ``RunMetrics.metrics`` dict
    (``services.run_reads.project_run_economics``); it surfaces no new data
    the runtime did not already persist. ``retry_rate`` is the only computed
    field — the per-phase retry surplus normalised by phase count:

        retry_rate = (sum(phase.attempts) - n_phases) / n_phases

    so a run where every phase ran exactly once has ``retry_rate == 0.0``,
    and the rate climbs by ``1/n_phases`` for each extra attempt. ``0.0``
    when there are no phases.
    """

    total_tokens: int = Field(
        default=0,
        description="Total tokens for the run (``metrics.total_tokens``).",
    )
    total_duration_s: float = Field(
        default=0.0,
        description="Total wall-clock seconds (``metrics.total_duration_s``).",
    )
    total_rounds: int = Field(
        default=0,
        description="Total pipeline rounds (``metrics.total_rounds``).",
    )
    retry_rate: float = Field(
        default=0.0,
        description="Per-phase retry surplus normalised by phase count; "
                    "``0.0`` when every phase ran exactly once.",
    )
    phases: list[PhaseCost] = Field(
        default_factory=list,
        description="Typed per-phase cost rows, in metrics phase order.",
    )


# ── orcho_run_events_tail ────────────────────────────────────────────────────


class EventRecord(BaseModel):
    """One event from events.jsonl. Mirrors the on-disk shape verbatim."""
    seq: int
    ts: str
    kind: str
    phase: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventsTailResult(BaseModel):
    run_id: str
    events: list[EventRecord]
    next_seq: int
    eof: bool = Field(
        description="True when no events remain after next_seq at this snapshot. "
                    "False means more events exist and another tail call will return them.",
    )


# ── orcho_skills_list ────────────────────────────────────────────────────────


class SkillRecord(BaseModel):
    """Wire-shape for a discovered Agent Skills package.

    Mirrors the portable :class:`pipeline.skills.SkillPackage` surface —
    skill metadata never carries runtime / model / provider fields, so
    consumers cannot mistakenly treat a skill as a runtime selector.
    """
    name: str
    description: str = ""
    source: str = "unknown"   # "project" | "workspace" | "user" | "package:<id>" | …
    checksum: str = ""
    root_dir: str = ""


class SkillsListResult(BaseModel):
    project_dir: str
    skills: list[SkillRecord]


# ── orcho_profiles_list ──────────────────────────────────────────────────────


class ProfileHypothesisRecord(BaseModel):
    """Plan-step hypothesis prelude policy exposed from v2 profiles."""
    attempts: int = Field(
        description="Number of hypothesis attempts before PLAN. Zero disables it.",
    )
    format: str | None = Field(
        default=None,
        description=(
            "Prompt format used for the hypothesis and its QA. None means "
            "inherit the owning plan step's prompt format."
        ),
    )


class ProfileRecord(BaseModel):
    """Profile catalogue entry.

    Supports both flat phase-list entries and v2 kind/variant/steps
    entries. v2 fields are optional and ``None`` for flat entries.
    """
    name: str
    phases: list[str] = Field(
        default_factory=list,
        description="v1 flat phase list. Empty when entry is v2-only.",
    )
    # v2 fields — populated when entry comes from pipeline_profiles_v2.json
    kind: str | None = Field(
        default=None,
        description="v2: 'full_cycle' | 'scoped' | 'custom'. None for v1 entries.",
    )
    variant: str | None = Field(
        default=None,
        description=(
            "v2: plugin/custom typology coordinate (the ``variant`` half of "
            "a ``kind × variant`` pair) used by custom or plugin-supplied "
            "profiles. Built-in profiles leave this ``null`` — their public "
            "identity is the semantic ``name`` / ``semantic_profile``, not a "
            "variant string."
        ),
    )
    description: str | None = Field(
        default=None,
        description="v2: human-readable purpose (one-line).",
    )
    semantic_profile: str | None = Field(
        default=None,
        description=(
            "v2: semantic work-kind identity for built-in profiles "
            "(e.g. ``feature``, ``small_task``, ``planning``, "
            "``code_review``). ``None`` for custom/plugin or internal "
            "profiles that carry no semantic identity."
        ),
    )
    default_mode: str | None = Field(
        default=None,
        description=(
            "v2: operating mode this profile defaults to "
            "(``fast`` / ``pro`` / ``governed``). ``None`` when the profile "
            "declares no default mode."
        ),
    )
    recipe_kind: str | None = Field(
        default=None,
        description=(
            "v2: recipe family — ``full_cycle`` (end-to-end), ``focused`` "
            "(scoped single-concern), or ``internal`` (engine-internal). "
            "``None`` when not declared."
        ),
    )
    internal: bool = Field(
        default=False,
        description=(
            "v2: ``True`` for engine-internal profiles (e.g. ``task`` / "
            "``correction``) that are not a normal public work-kind choice. "
            "The catalogue still lists them so callers can resolve them, but "
            "clients should not offer them as a primary selection."
        ),
    )
    cross_gates: dict[str, dict] | None = Field(
        default=None,
        description=(
            "v2: profile-level policy for runner-owned cross gates "
            "(``contract_check`` / ``cross_final_acceptance``). Each "
            "entry is a JSON object with ``enabled``, ``run``, "
            "``on_skip``, and optional ``mode``. None when the profile "
            "does not declare an explicit ``cross_gates`` block — "
            "consumers should treat missing as 'use documented "
            "defaults', not as 'no policy'."
        ),
    )
    hypothesis: ProfileHypothesisRecord | None = Field(
        default=None,
        description=(
            "v2: plan-step hypothesis prelude policy. None when the profile "
            "has no plan-step hypothesis block or sets attempts=0."
        ),
    )


class ProfileSelectorRecord(BaseModel):
    """A profile *selector* — a ``profile`` value that picks a profile
    dynamically rather than naming an executable recipe.

    Distinct from :class:`ProfileRecord` (an executable profile with a
    concrete phase recipe): a selector is consumed by orcho-core *before*
    profile resolution to choose a semantic profile. ``auto-detect`` is the
    canonical example — core classifies the work kind and selects the
    matching semantic profile + mode. Surfaced separately from
    ``profiles`` so a client never tries to run a selector as if it were a
    recipe, and never mistakes it for a missing executable profile.
    """
    name: str = Field(
        description="The selector token to pass as ``profile`` (e.g. "
                    "``auto-detect``).",
    )
    description: str = Field(
        description="One-line purpose of the selector.",
    )
    is_selector: Literal[True] = Field(
        default=True,
        description="Always ``True`` — marks this entry as a dynamic "
                    "selector, not an executable profile recipe.",
    )


class ProfilesListResult(BaseModel):
    profiles: list[ProfileRecord]
    selectors: list[ProfileSelectorRecord] = Field(
        default_factory=list,
        description=(
            "Profile *selectors* — ``profile`` values that pick a profile "
            "dynamically (e.g. ``auto-detect``: core classifies the work "
            "kind and selects the matching semantic profile) rather than "
            "naming an executable recipe. Kept disjoint from ``profiles``: "
            "a selector never appears in ``profiles`` and vice versa. "
            "Surfaced even when ``source='missing'`` because selectors do "
            "not depend on the v2 catalogue file."
        ),
    )
    source: str = Field(
        description=(
            '"json_v2" when the v2 profile file '
            '(_config/pipeline_profiles_v2.json) was loaded; "missing" '
            "when no v2 file is present (orcho-core is required to "
            "supply one — empty result + diagnostic)."
        ),
    )
    diagnostic: str | None = Field(
        default=None,
        description=(
            "Human-readable explanation when ``source != 'json_v2'``. "
            "MCP clients SHOULD surface this to the user when present. "
            "None when profiles loaded successfully."
        ),
    )


__all__ = [
    "AutoDetectProjection",
    "EventRecord",
    "EventsTailResult",
    "FollowupLineage",
    "HistoryResult",
    "PhaseCost",
    "ProfileHypothesisRecord",
    "ProfileRecord",
    "ProfileSelectorRecord",
    "ProfilesListResult",
    "RecoveryRecommendation",
    "RunEconomics",
    "RunMetrics",
    "RunRecord",
    "RunStatus",
    "SkillRecord",
    "SkillsListResult",
    "WorktreeContinuity",
]
