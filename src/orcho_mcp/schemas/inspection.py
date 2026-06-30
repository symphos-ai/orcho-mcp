"""orcho_mcp.schemas.inspection — wire models for inspection tools.

Covers ``orcho_run_evidence`` (sliced evidence bundle: plan, findings,
commands, artifacts, errors, sub_runs) and ``orcho_run_diff`` (per-file
+A/-R summary plus optional content). Both are read-only forensic
surfaces; ``services/run_artifacts.py`` (resources path) and
``inspection/`` (tool path) are the implementation homes.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from orcho_mcp.schemas.shared import NextActionRecord, ProviderPressure

# ── orcho_run_evidence ─────────────────────────────────────────────────────


class FindingRecord(BaseModel):
    """One reviewer finding flattened from a phase attempt."""
    id: str
    severity: str
    title: str
    body: str
    required_fix: str | None = None
    file: str | None = None
    line: int | None = None
    phase: str
    attempt: int


class PlanSliceRecord(BaseModel):
    """Compact plan projection — short enough for an LLM context window."""
    source: str
    short_summary: str
    planning_context: str
    subtask_count: int
    has_contract: bool
    goal: str | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    owned_files: list[str] = Field(default_factory=list)
    commands_to_run: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    review_focus: list[str] = Field(default_factory=list)


class EvidenceCommandSliceRecord(BaseModel):
    argv_summary: str
    cwd: str
    exit_code: int | None = None
    duration_s: float
    outcome: str


class EvidenceArtifactSliceRecord(BaseModel):
    path: str
    kind: str
    size_bytes: int


class ImplementDeliveryRecord(BaseModel):
    """First-class typed projection of the implement delivery / waiver audit.

    The delivery/waiver audit fields are surfaced as a typed projection on
    the ``errors`` evidence slice — its natural home, since the breadcrumbs
    arrive in the same errors-rollup. The projector in
    ``inspection/evidence.py`` builds this from the already-fetched
    ``eh.errors`` list, never from a second raw-meta read, so the typed
    record cannot drift from the raw breadcrumbs. ``RunStatus.meta`` carries
    the same scalar audit fields in its summary projection
    (``meta.phases.implement`` + ``meta.phase_handoff_waiver``), and callers
    that need the full persisted meta can pass ``include=["all"]``; this
    record does not replace it.

    Sourced by merging two rollup breadcrumb dicts:
      - ``kind == 'implement_delivery'`` → ``delivery_status``,
        ``delivery_waived``, ``waiver_id``, ``action``,
        ``incomplete_subtasks``, ``missing_subtask_receipts``;
      - ``kind == 'phase_handoff_waiver'`` → ``decided_by``.
    """
    delivery_status: str = Field(
        description="'clean' | 'repaired' | 'waived' | 'incomplete'.",
    )
    delivery_waived: bool = False
    waiver_id: str | None = None
    action: str | None = Field(
        default=None,
        description="'continue' | 'continue_with_waiver'.",
    )
    decided_by: str | None = Field(
        default=None,
        description="'operator' | 'auto:on_exhausted'.",
    )
    incomplete_subtasks: list[str] = Field(default_factory=list)
    missing_subtask_receipts: list[str] = Field(default_factory=list)


class ErrorsHaltSliceRecord(BaseModel):
    status: str
    errors: list[dict[str, Any]] = Field(default_factory=list)
    halt_reason: str | None = None
    halted_at: str | None = None
    error_summary: str | None = None
    implement_delivery: ImplementDeliveryRecord | None = Field(
        default=None,
        description=(
            "Typed delivery/waiver audit projected from the same "
            "errors-rollup. None for a clean delivery (no "
            "'implement_delivery' breadcrumb in the rollup)."
        ),
    )
    provider_pressure: ProviderPressure | None = Field(
        default=None,
        description=(
            "Core-typed provider runtime/access failure projected from the "
            "same ``project_provider_pressure`` source (and the same shared "
            "``build_provider_pressure_next_actions`` helper) as "
            "``orcho_run_status`` / ``orcho_run_diagnose`` / "
            "``orcho_run_events_summary`` — so the errors slice never loses "
            "the core fact and reports an identical condition / failure_kind / "
            "phase / next_actions. ``None`` when core attached no typed "
            "provider failure."
        ),
    )


class SubRunLinkRecord(BaseModel):
    name: str
    status: str | None = None
    run_dir: str


class CriterionReportRecord(BaseModel):
    """One developer claim against a subtask done-criterion (P7).

    ``met`` is the developer's explicit self-attestation and ``evidence`` is a
    one-sentence claim — NOT verified truth. The reviewer / final_acceptance /
    test gates remain the verification layer; this record only reports what was
    claimed.
    """
    index: int = Field(
        description="1-based position in the subtask's declared done_criteria.",
    )
    criterion: str
    met: bool
    evidence: str


class SubtaskReceiptRecord(BaseModel):
    """One subtask's terminal delivery receipt for a ``subtask_dag`` run.

    ``state`` is ``"done" | "incomplete" | "failed" | "skipped"``.
    ``"incomplete"`` means execution succeeded but the typed done-criteria
    self-attestation was missing / malformed / mismatched / not-all-met —
    distinct from a hard ``"failed"`` execution error. ``criteria_report`` /
    ``attestation_summary`` / ``attestation_error`` carry the P7 attestation
    and are empty / ``None`` for criteria-less subtasks and pre-P7 runs.
    """
    subtask_id: str
    state: str
    runtime: str = ""
    model: str = ""
    skill: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    done_criteria: list[str] = Field(default_factory=list)
    duration: float = 0.0
    error: str | None = None
    criteria_report: list[CriterionReportRecord] = Field(default_factory=list)
    attestation_summary: str | None = None
    attestation_error: str | None = Field(
        default=None,
        description=(
            "Why the done-criteria attestation gate did not close, when "
            "``state == 'incomplete'``. None on a clean ``done`` receipt."
        ),
    )
    attestation_repaired: bool = Field(
        default=False,
        description=(
            "True when Orcho recovered a malformed machine-readable "
            "attestation with one non-mutating repair turn."
        ),
    )


class VerificationCheckRecord(BaseModel):
    """One environment check from a verification-environment receipt.

    The canonical check is ``pipeline_import``: it proves which
    ``pipeline/__init__.py`` the phase's interpreter actually imported.
    ``expected`` / ``actual`` are absolute paths (``None`` when the check
    could not be confirmed); ``passed`` is True only when they matched.
    """
    name: str
    expected: str | None = None
    actual: str | None = None
    passed: bool = False


class VerificationCommandRecord(BaseModel):
    """One command a verification-environment receipt recorded.

    ``argv`` is the exact command line (normalised to a list of strings);
    ``exit_code`` is its return code (``None`` when the subprocess could
    not be launched / timed out).
    """
    argv: list[str] = Field(default_factory=list)
    exit_code: int | None = None


class VerificationReceiptRecord(BaseModel):
    """A durable verification-environment receipt.

    A developer-side phase (``implement`` / ``repair_changes``) records
    *where* it ran its work and checks: which interpreter (``python``),
    which working directory (``cwd``), the import checks it ran, the exact
    commands + exit codes, and whether any throwaway environment lived
    outside the source checkout (``temp_env_outside_checkout`` — the
    clean-tree note). ``all_passed`` rolls up the checks; ``artifact_path``
    is the on-disk receipt path under the run directory.
    """
    phase: str | None = None
    round: int | None = None
    kind: str = "verification_environment"
    python: str | None = Field(
        default=None,
        description="Interpreter identity: ``<version> (<executable>)``.",
    )
    cwd: str | None = Field(
        default=None,
        description="Working directory the phase ran its checks from.",
    )
    checks: list[VerificationCheckRecord] = Field(default_factory=list)
    commands: list[VerificationCommandRecord] = Field(default_factory=list)
    temp_env_outside_checkout: bool = Field(
        default=True,
        description="Clean-tree note: True when any throwaway environment "
                    "lived outside the source checkout (so it cannot pollute "
                    "``git status`` in the checkout).",
    )
    all_passed: bool = Field(
        default=False,
        description="True when every recorded check passed.",
    )
    artifact_path: str | None = Field(
        default=None,
        description="On-disk path of the receipt JSON under the run "
                    "directory (``<run_dir>/verification_receipts/...``).",
    )


class VerificationTimelineGateRecord(BaseModel):
    """One required delivery command's official verification-gate state.

    Projected verbatim from the SDK's durable
    ``sdk.get_verification_timeline`` per-gate record. ``status`` is EXACTLY
    one of the six values ``PASS`` / ``FAIL`` / ``MISSING`` / ``STALE`` /
    ``SKIPPED`` / ``FRESH`` — there is no ``MANUAL`` value. A manual /
    operator-only gate is surfaced as ``status='SKIPPED'`` with
    ``policy='manual_only'`` and membership in the timeline's ``manual_only``,
    never as missing.

    ``searched_run_dirs`` and ``rerun_hint`` are populated ONLY for a
    non-present required gate (``MISSING`` / ``STALE`` / ``FAIL``); present and
    manual gates carry empty lists. ``inherited`` is True when the deciding
    receipt came from a parent run (``source_run_id`` differs from this run).
    """
    command: str
    env: str | None = None
    hook: str | None = Field(
        default=None,
        description="Delivery hook the gate is scheduled at "
                    "(e.g. ``after_phase(implement)``, ``before_delivery``).",
    )
    source: str | None = None
    policy: str | None = Field(
        default=None,
        description="Effective delivery policy "
                    "(``require`` / ``warn`` / ``suggest`` / ``manual_only``).",
    )
    required: bool = False
    status: Literal["PASS", "FAIL", "MISSING", "STALE", "SKIPPED", "FRESH"]
    receipt_path: str | None = None
    source_run_id: str | None = None
    inherited: bool = False
    stale_reason: str | None = None
    searched_run_dirs: list[str] = Field(default_factory=list)
    rerun_hint: list[str] = Field(default_factory=list)
    detail: str | None = Field(
        default=None,
        description="Human-readable operator note, populated when an "
                    "environment-provenance break downgrades the gate to "
                    "``FAIL`` — names the failing verification_environment check "
                    "with its expected/actual "
                    "(e.g. ``pipeline_import: expected <X> actual <Y>``). "
                    "``None`` for an ordinary gate.",
    )


class VerificationAutorunEventRecord(BaseModel):
    """One durable auto-run firing mirrored from the run's phase log.

    ``ran_pass`` are commands executed and passing this firing; ``ran_fail``
    those that failed; ``skipped_fresh`` were already present (fresh receipt,
    not executed); ``skipped_manual`` were intentionally not auto-run.
    ``receipt_paths`` are the durable env / command receipts this firing wrote.
    """
    hook_label: str
    source: str
    ran_pass: list[str] = Field(default_factory=list)
    ran_fail: list[str] = Field(default_factory=list)
    skipped_fresh: list[str] = Field(default_factory=list)
    skipped_manual: list[str] = Field(default_factory=list)
    receipt_paths: list[str] = Field(default_factory=list)


class VerificationTimelineRecord(BaseModel):
    """Typed projection of a run's official verification-gate timeline.

    Read-only durable source of truth from ``sdk.get_verification_timeline``:
    per-gate status + remediation (``gates``), the aggregate residual / manual /
    inherited sets, the shared ``suggested_commands`` hints, and the durable
    auto-run events. ``scheduled_trail_available`` is ``False`` until core
    persists a durable per-firing scheduled-gate trail — see GC-10 in
    ``docs/ux/flagship_gap_spec.md`` for the exact core follow-up gap.
    """
    run_id: str
    has_contract: bool = False
    gates: list[VerificationTimelineGateRecord] = Field(default_factory=list)
    residual_missing: list[str] = Field(default_factory=list)
    residual_stale: list[str] = Field(default_factory=list)
    residual_failed: list[str] = Field(default_factory=list)
    manual_only: list[str] = Field(default_factory=list)
    inherited: list[str] = Field(default_factory=list)
    searched_run_dirs: list[str] = Field(default_factory=list)
    suggested_commands: list[str] = Field(default_factory=list)
    autorun_events: list[VerificationAutorunEventRecord] = Field(
        default_factory=list,
    )
    scheduled_trail_available: bool = False


class VerificationGateCockpitRow(BaseModel):
    """One verification gate as a cockpit row — a typed, actionable

    projection of a single ``VerificationTimelineGateRecord`` that makes the
    gate's *planning* properties (how it fires, who owns it, what it gates on)
    legible alongside its status, without collapsing any of them.

    ``trigger`` is derived deterministically (never read from core):

      - ``operator_only`` — the command is in the timeline's ``manual_only``
        set OR ``policy == 'manual_only'``. A manual / operator-only gate is
        present here on purpose; ``status='SKIPPED'`` for it is NOT an
        automation failure and must not be read as missing.
      - ``auto`` — the command appears in an autorun event's ``ran_pass`` /
        ``ran_fail`` / ``skipped_fresh`` (the run's automation acted on it).
      - ``manual`` — neither of the above (a declared gate the automation has
        not acted on and that is not operator-only).

    ``class_source`` honours provenance: ``'core'`` ONLY when ``gate_class``
    came from a durable core field. When the class is inferred locally it is
    ``'derived'``; when there is no class signal at all it is ``'unspecified'``.
    ``'core'`` must never be claimed for a guessed taxonomy.

    ``status`` is the same six-value enum as the timeline
    (``PASS`` / ``FAIL`` / ``MISSING`` / ``STALE`` / ``SKIPPED`` / ``FRESH``);
    there is no ``MANUAL`` status — manual is expressed via
    ``trigger`` + ``policy``. ``inherited`` / ``source_run_id`` / ``receipt_path``
    carry the deciding-receipt evidence; ``stale_reason`` / ``rerun_hint`` are
    populated where applicable.
    """
    command: str
    hook: str | None = Field(
        default=None,
        description="Delivery hook / phase the gate is scheduled at "
                    "(e.g. ``after_phase(implement)``, ``before_delivery``).",
    )
    trigger: Literal["auto", "manual", "operator_only"]
    policy: str | None = None
    required: bool = False
    gate_class: str | None = None
    class_source: Literal["core", "derived", "unspecified"] = "unspecified"
    status: Literal["PASS", "FAIL", "MISSING", "STALE", "SKIPPED", "FRESH"]
    env: str | None = None
    receipt_path: str | None = None
    inherited: bool = False
    source_run_id: str | None = None
    stale_reason: str | None = None
    rerun_hint: list[str] = Field(default_factory=list)
    detail: str | None = Field(
        default=None,
        description="Human-readable operator note, populated when an "
                    "environment-provenance break downgrades the gate to "
                    "``FAIL`` — names the failing verification_environment check "
                    "with its expected/actual "
                    "(e.g. ``pipeline_import: expected <X> actual <Y>``). "
                    "``None`` for an ordinary gate.",
    )


class VerificationCockpit(BaseModel):
    """Typed cockpit projection of a run's verification gates.

    A read-only, actionable view derived from the SAME SDK call that feeds
    ``VerificationTimelineRecord`` — it augments the timeline, never replaces
    or hides it. The header carries contract presence, the (best-effort) work
    mode, the environments seen across gates, an aggregate ``policy_summary``,
    and a human-readable ``effect`` string. ``gates`` is the per-gate cockpit
    rows; the residual / manual-only / inherited aggregates mirror the
    timeline's so a manual-only ``SKIPPED`` gate never lands in
    residual missing / failed.

    ``has_contract`` is ALWAYS set explicitly by the builder from
    ``proj.has_contract`` (the default here exists only so the constructor is
    usable). ``mode`` stays ``None`` until core exposes ``work_mode`` through the
    timeline projection.
    """
    run_id: str
    has_contract: bool = False
    mode: str | None = None
    envs: list[str] = Field(default_factory=list)
    policy_summary: str = Field(
        description="Aggregate of gate policies: "
                    "'require' | 'warn' | 'suggest' | 'mixed' | 'none'.",
    )
    effect: str = Field(
        description="Human-readable derived effect of the policy aggregate.",
    )
    gates: list[VerificationGateCockpitRow] = Field(default_factory=list)
    residual_missing: list[str] = Field(default_factory=list)
    residual_stale: list[str] = Field(default_factory=list)
    residual_failed: list[str] = Field(default_factory=list)
    manual_only: list[str] = Field(default_factory=list)
    inherited: list[str] = Field(default_factory=list)
    suggested_commands: list[str] = Field(default_factory=list)


class HandoffAdviceUsageRecord(BaseModel):
    """Aggregated advisor usage across a run's handoff-advice calls.

    Every field is ``None`` when no call carried that signal — never a
    fabricated zero (an absent cost is meaningful: cost unknown). Mirrors the
    SDK ``HandoffAdviceUsage`` projection verbatim.
    """
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_cached: int | None = None
    duration_s: float | None = None
    cost_usd_equivalent: float | None = None


class HandoffAdviceCallRecord(BaseModel):
    """One Stage 0/1 advisor invocation, projected from a durable advice artifact.

    A 1:1 typed view of one entry in the SDK ``list_handoff_advice`` projection
    (itself a verbatim wrapper over ``collect_handoff_advice``). The outcome
    classification (``resolved`` / ``repeated`` / ``outcome``) is the
    normalizer's — this wire model adds no policy. ``advice_artifact`` is the
    run-relative path to the advice JSON; the usage fields are ``None`` when the
    artifact carried no accounting.

    ``resolved`` is tri-state: ``True`` (the retry cleared the finding),
    ``False`` (it recurred), or ``None`` (unknown / not an applied retry).
    """
    handoff_id: str
    phase: str
    advice_artifact: str
    trigger: str
    verdict: str
    feedback_source: str | None = None
    recommended_action: str
    applied_action: str | None = None
    confidence: str
    finding_fingerprint: str = ""
    resolved: bool | None = None
    repeated: bool = False
    outcome: str
    severity_counts: dict[str, int] = Field(default_factory=dict)
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_cached: int | None = None
    duration_s: float | None = None
    cost_usd_equivalent: float | None = None
    model: str | None = None


class HandoffAdviceSummaryRecord(BaseModel):
    """Run-level rollup over the handoff-advice calls (mirrors the SDK summary)."""
    calls: int = 0
    applied_retries: int = 0
    resolved_retries: int = 0
    repeated: int = 0
    stopped: int = 0
    unknown: int = 0
    usage: HandoffAdviceUsageRecord | None = None


class HandoffAdviceSliceRecord(BaseModel):
    """Typed projection of a run's Stage 0/1 handoff-advice evidence surface.

    Folds the durable ``phase_handoff_advice/`` artifacts, their matching
    ``phase_handoff_decisions/`` provenance, and the per-phase verdicts into a
    per-call list plus an aggregate summary. A run with no Stage 0/1 advisor
    surface yields an empty slice (``calls=[]`` and a zeroed summary), never an
    error — read-only forensic data the operator can correlate with the
    ``orcho_handoff_advice`` recommendation tool.
    """
    calls: list[HandoffAdviceCallRecord] = Field(default_factory=list)
    summary: HandoffAdviceSummaryRecord = Field(
        default_factory=HandoffAdviceSummaryRecord,
    )


class EvidenceResult(BaseModel):
    """Returned by ``orcho_run_evidence``.

    The ``slice`` field echoes back the requested view; the matching
    field on the result is populated, others stay ``None``. ``slice="all"``
    populates every slice for one-shot inspection.

    Slices:
      - ``"plan"`` — plan summary (PlanSliceRecord)
      - ``"findings"`` — flattened reviewer findings (list)
      - ``"commands"`` — pipeline shell-outs (list)
      - ``"artifacts"`` — files the run wrote (list)
      - ``"errors"`` — errors + halt reason (ErrorsHaltSliceRecord)
      - ``"sub_runs"`` — cross-run child aliases (list)
      - ``"receipts"`` — per-subtask delivery receipts incl. done-criteria
        attestation (list of SubtaskReceiptRecord)
      - ``"verification_receipts"`` — durable verification-environment
        receipts (interpreter / cwd / import checks / commands / exit
        codes / clean-tree note) (list of VerificationReceiptRecord)
      - ``"verification_timeline"`` — official verification-gate timeline:
        per-gate status (six-value enum, no MANUAL) with per-gate
        rerun_hint / searched_run_dirs, the residual / manual-only /
        inherited aggregates, and the auto-run events
        (VerificationTimelineRecord)
      - ``"verification_cockpit"`` — typed cockpit projection of the SAME
        timeline (one SDK read): header (has_contract / mode / envs /
        policy_summary / effect) plus per-gate cockpit rows that surface
        trigger (auto / manual / operator_only), policy, requiredness, gate
        class + provenance, status, and evidence (VerificationCockpit)
      - ``"handoff_advice"`` — Stage 0/1 phase-handoff advisor evidence:
        per-call records (handoff_id / phase / recommended_action /
        applied_action / confidence / resolved / repeated / outcome /
        finding_fingerprint / usage+cost / advice_artifact) plus an aggregate
        summary (calls / applied_retries / resolved_retries / repeated /
        stopped / unknown / usage). Empty slice when the run has no advisor
        surface (HandoffAdviceSliceRecord)
      - ``"all"`` — every slice in one response
    """
    run_id: str
    slice: str
    plan: PlanSliceRecord | None = None
    findings: list[FindingRecord] | None = None
    commands: list[EvidenceCommandSliceRecord] | None = None
    artifacts: list[EvidenceArtifactSliceRecord] | None = None
    errors: ErrorsHaltSliceRecord | None = None
    sub_runs: list[SubRunLinkRecord] | None = None
    receipts: list[SubtaskReceiptRecord] | None = None
    verification_receipts: list[VerificationReceiptRecord] | None = None
    verification_timeline: VerificationTimelineRecord | None = None
    verification_cockpit: VerificationCockpit | None = None
    handoff_advice: HandoffAdviceSliceRecord | None = None


# ── orcho_run_diff ───────────────────────────────────────────────────────────


class RunDiffFile(BaseModel):
    """Per-file +A -R summary in a :class:`RunDiffResult`."""
    path: str
    added: int
    removed: int


class RunDiffResult(BaseModel):
    """Returned by ``orcho_run_diff``.

    ``found=False`` (with ``files=[]`` and ``content=""``) signals "no
    diff artifact recorded for this run / phase" — typed not-found,
    not a JSON-RPC error.

    ``found=True`` with empty ``files`` signals "path filter matched
    nothing"; ``message`` quotes the filter.

    ``mode`` is constrained to the literal set so the published JSON
    Schema enumerates the choices, not a free ``str``. ``max_bytes``
    echoes the cap that produced ``truncated`` so clients can render
    a footer without an out-of-band argument.

    ``scope`` echoes which artifact was read: ``"run"`` for the
    cumulative ``diff.patch`` (default behaviour), ``"phase"`` for a
    per-phase ``phases/<phase>/diff.patch``. ``phase`` carries the
    normalized phase name on phase calls, ``None`` on run calls — so
    clients don't have to remember what they asked for.
    """
    run_id: str
    found: bool
    mode: Literal["preview", "stat", "full"]
    diff_path: str | None = None
    files: list[RunDiffFile] = Field(default_factory=list)
    content: str = ""
    truncated: bool = False
    max_bytes: int | None = None
    message: str | None = None
    scope: Literal["run", "phase"] = "run"
    phase: str | None = None


# ── delivery / correction gate projection ───────────────────────────────────


class DeliveryGateDiffSummary(BaseModel):
    """Diff summary for an Orcho-managed delivery / correction gate.

    Built first from the authoritative ``meta`` commit-delivery decision
    (``changed_paths`` / ``untracked_paths``); the durable
    ``commit_decisions`` artifact and ``diff.patch`` only enrich it.
    ``degraded`` is ``True`` when one of those *secondary* artifacts is
    missing or unreadable — the gate ``kind`` is never affected by that,
    only this summary's completeness (the projection message names the
    missing artifact).
    """
    files_changed: int = 0
    changed_paths: list[str] = Field(default_factory=list)
    untracked_paths: list[str] = Field(default_factory=list)
    degraded: bool = Field(
        default=False,
        description=(
            "True when a secondary artifact (commit_decisions / diff.patch) "
            "was missing or unreadable, so this summary is partial. The gate "
            "kind is unaffected — it is derived only from the authoritative "
            "meta commit-delivery status."
        ),
    )


class DeliveryActionRecord(BaseModel):
    """One delivery / correction action the operator may take at the gate.

    ``creates_commit`` is the load-bearing flag: only ``approve`` writes a
    new commit to the target checkout. ``apply`` lands the diff uncommitted;
    ``fix`` / ``skip`` / ``halt`` never commit. ``effect`` is a one-line
    human-readable description of what choosing the action does.
    """
    action: str
    effect: str
    creates_commit: bool = False


class DeliveryGateProjection(BaseModel):
    """Typed projection of an Orcho-managed post-release delivery gate.

    ``kind`` is the single classification signal and is derived ONLY from
    the authoritative ``meta`` commit-delivery ``status`` (never from
    terminal log prose):

      - ``delivery_decision_required`` — a ``pending`` delivery on an
        APPROVED release; the operator chooses approve / apply / skip / halt.
      - ``correction_decision_required`` — a rejected release or a
        ``fix_requested`` state; the operator normally chooses fix / halt for a
        current rejected release. This is an *available correction-flow state*,
        NOT an executed delivery.
      - ``direct_checkout_or_running`` — no pending commit-delivery gate in
        meta (terminal delivery, or a direct checkout edit / still-running
        run); no approve / apply / fix is offered.

    ``available_actions`` comes from orcho-core's read-only
    ``delivery_decision_state`` surface and lists only actions the SDK says are
    currently safe. ``blocked_actions`` names delivery actions currently
    refused by hard guards such as a rejected release verdict or incomplete
    required verification. ``next_actions`` carries one ``ready_call`` to
    ``orcho_delivery_decide`` per available action.
    """
    run_id: str
    kind: Literal[
        "delivery_decision_required",
        "correction_decision_required",
        "direct_checkout_or_running",
    ]
    release: Literal["approved", "rejected", "none"] = Field(
        default="none",
        description="Release verdict outcome from meta ``release_verdict``.",
    )
    target_checkout: str | None = Field(
        default=None,
        description="The checkout a delivery would land in (run project path).",
    )
    retained_worktree: str | None = Field(
        default=None,
        description="The retained source worktree holding the change.",
    )
    diff: DeliveryGateDiffSummary = Field(default_factory=DeliveryGateDiffSummary)
    default_action: str | None = Field(
        default=None,
        description="The resolved default action from meta (e.g. approve / fix).",
    )
    available_actions: list[DeliveryActionRecord] = Field(default_factory=list)
    blocked_actions: list[str] = Field(
        default_factory=list,
        description=(
            "Actions core currently refuses for this gate, usually shipping "
            "actions blocked by release, required-verification, or "
            "delivery-scope guards."
        ),
    )
    scope_blocker: str | None = Field(
        default=None,
        description=(
            "Set to ``delivery_scope_violation`` when shipping is refused "
            "because the run resolved under strict mono but sibling-repo "
            "changes were found outside its delivery scope. ``None`` for any "
            "gate not blocked by a delivery-scope violation."
        ),
    )
    scope_disclosure: list[str] = Field(
        default_factory=list,
        description=(
            "Per-alias sibling files (``[alias]/rel/path``) implicated by a "
            "delivery-scope decision — the concrete changes behind a "
            "``delivery_scope_violation`` block. Empty for a gate with no "
            "delivery-scope dimension."
        ),
    )
    message: str | None = None
    next_actions: list[NextActionRecord] = Field(default_factory=list)


__all__ = [
    "CriterionReportRecord",
    "DeliveryActionRecord",
    "DeliveryGateDiffSummary",
    "DeliveryGateProjection",
    "ErrorsHaltSliceRecord",
    "EvidenceArtifactSliceRecord",
    "EvidenceCommandSliceRecord",
    "EvidenceResult",
    "FindingRecord",
    "HandoffAdviceCallRecord",
    "HandoffAdviceSliceRecord",
    "HandoffAdviceSummaryRecord",
    "HandoffAdviceUsageRecord",
    "ImplementDeliveryRecord",
    "PlanSliceRecord",
    "RunDiffFile",
    "RunDiffResult",
    "SubRunLinkRecord",
    "SubtaskReceiptRecord",
    "VerificationCheckRecord",
    "VerificationCockpit",
    "VerificationCommandRecord",
    "VerificationGateCockpitRow",
    "VerificationReceiptRecord",
]
