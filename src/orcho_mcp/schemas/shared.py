"""orcho_mcp.schemas.shared — cross-domain wire models.

Models used by more than one tool family live here so domain modules
don't import from each other. ``NextActionRecord`` is the only entry
today — it rides in run-status, run-start, and phase-handoff-decide
responses, so it would create a cross-domain edge if it lived in any
one of them.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class NextActionRecord(BaseModel):
    """One suggested follow-up tool call surfaced in tool responses.

    Workflow-decision responses carry these records so the LLM does not
    have to remember workflow patterns from documentation; the suggested
    next steps ride in the payload.

    Mirrors :class:`sdk.actions.Action` on the orcho-core side. The base
    pass-through (``intent``/``tool``/``args``/``optional``) stays
    state-derived and drift-free; the ``kind`` family below is MCP-wire
    enrichment computed entirely on the MCP side from state the server
    already has, so it requires no new data from the core and the
    defaults reproduce the historical pass-through record exactly.

    Call-readiness invariant (checked via the typed ``kind`` field, never
    by parsing ``intent``):

    - ``kind='ready_call'`` ⇒ ``args`` already contains every required
      parameter of the target tool's actual signature (e.g.
      ``orcho_run_resume`` ⇒ ``args`` has ``run_id``;
      ``orcho_phase_handoff_decide`` ⇒ ``args`` has ``action`` and, where
      the chosen verb mandates it, ``feedback``). The record is safe to
      forward verbatim.
    - ``kind='operator_input_required'`` ⇒ the final decision args MAY be
      omitted, but ONLY when ``choices`` and/or ``input_schema`` are
      present to tell the operator what input is still needed.

    ``intent`` stays human-readable and is NOT a contractual signal.
    """
    intent: str = Field(
        description=(
            "One-sentence human-readable description of what this "
            "action would accomplish. Clients SHOULD surface this "
            "verbatim in their UI."
        ),
    )
    tool: str = Field(
        description=(
            "Name of the MCP tool the caller should invoke (e.g. "
            "``orcho_run_start``, ``orcho_phase_handoff_decide``, "
            "``orcho_run_resume``)."
        ),
    )
    args: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Pre-filled arguments matching the target tool's input "
            "schema. Values are safe to forward as-is. When a "
            "follow-up requires operator input, that input is not "
            "represented as a placeholder inside this dict."
        ),
    )
    optional: bool = Field(
        default=True,
        description=(
            "True (default) when this is one of several valid "
            "follow-ups. False when the workflow has a single "
            "deterministic next step (e.g. ``orcho_run_resume`` "
            "after ``continue`` / ``retry_feedback`` / "
            "``continue_with_waiver`` decision)."
        ),
    )
    kind: Literal["ready_call", "operator_input_required"] = Field(
        default="ready_call",
        description=(
            "Typed call-readiness signal. ``ready_call`` (default) means "
            "``args`` already carries every required parameter of the "
            "target tool, so the record is safe to forward verbatim. "
            "``operator_input_required`` means a final decision argument "
            "is intentionally omitted and the caller must collect operator "
            "input first, as described by ``choices`` and/or "
            "``input_schema``. This typed field — not ``intent`` text — is "
            "the contractual readiness signal."
        ),
    )
    requires_operator_input: bool = Field(
        default=False,
        description=(
            "True iff ``kind == 'operator_input_required'``: the caller "
            "must gather operator input before invoking the tool. False "
            "(default) for ready-to-forward calls."
        ),
    )
    choices: list[str] | None = Field(
        default=None,
        description=(
            "When operator input is a choice among a fixed set (e.g. the "
            "available phase-handoff decision verbs), the allowed values. "
            "``None`` (default) for ready-to-forward calls."
        ),
    )
    input_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "When operator input is free-form (e.g. required retry "
            "``feedback``), a description of the expected input. ``None`` "
            "(default) for ready-to-forward calls."
        ),
    )
    context: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Structured, machine-readable recovery context for this action "
            "that is NOT a parameter of the target tool (so it must not ride "
            "in ``args``), yet a typed client needs to branch on it. The "
            "from_run_plan follow-up after a rejected final acceptance "
            "carries here the parent's retained ``diff_path`` and its "
            "``project_dir`` / ``retained_worktree`` checkout context, plus "
            "the ``from_run_plan`` parent id. Read these typed keys — never "
            "the ``intent`` prose — for the diff/worktree pointers. ``None`` "
            "(default) when the action carries no extra context."
        ),
    )


# ── Recovery-lineage vocabulary (shared by diagnose + status) ────────────────
#
# The typed continuation vocabulary projected from ``services.run_lineage``.
# Defined here, in the cross-domain module, so both ``run_control.RunDiagnosis``
# and ``read.RunStatus`` reference one source of truth without importing from
# each other. The string members mirror
# ``services.run_lineage.ContinuationSubject`` /
# ``services.run_lineage.RecommendedNextAction`` one-for-one.

ContinuationSubjectLiteral = Literal[
    "source_run_checkpoint",
    "active_child_run",
    "delivery_gate",
    "plan_artifact",
    "none",
    "unknown",
]
RecommendedNextActionLiteral = Literal[
    "resume_source_run",
    "resume_active_child",
    "delivery_decision",
    "start_followup",
    "plan_artifact_continuation",
    "stop_unknown",
]


class RecoveryLineage(BaseModel):
    """Durable recovery-lineage facts behind a continuation recommendation.

    The structured facts a captain can branch on without reading output logs:
    which source run was resolved and whether it is checkpoint-resumable,
    whether an active follow-up child exists, whether a persisted plan
    artifact is the subject, and — for an ``unknown`` dead-end — exactly which
    durable facts are missing. All fields are projected from ``meta`` / SDK
    readers (never parsed from log prose), so a missing fact reads as
    ``None`` / ``False`` / an explicit ``missing_facts`` entry.

    Reused verbatim by ``RunDiagnosis`` (``orcho_run_diagnose``) and
    ``RecoveryRecommendation`` (``orcho_run_status``) so the two surfaces never
    drift.
    """

    source_run_id: str | None = Field(
        default=None,
        description="The resolved source/parent run a terminal recovery run "
                    "should continue, when one is durably linked; ``None`` "
                    "otherwise.",
    )
    source_status: str | None = Field(
        default=None,
        description="Merged status of ``source_run_id`` when readable.",
    )
    source_resumable: bool = Field(
        default=False,
        description="True when the source run is NOT a terminal-resume-parent "
                    "and still owns retained work (a preserved worktree or a "
                    "persisted plan) — i.e. resuming it would advance the "
                    "change session.",
    )
    active_child_run_id: str | None = Field(
        default=None,
        description="The newest unfinished follow-up child of the inspected "
                    "run, when one supersedes it; ``None`` otherwise.",
    )
    plan_subject_available: bool = Field(
        default=False,
        description="True when a durable, readable ``parsed_plan.json`` "
                    "artifact is the continuation subject (a plan-only / "
                    "research run with no undelivered diff).",
    )
    missing_facts: list[str] = Field(
        default_factory=list,
        description="For an ``unknown`` dead-end, the exact durable facts that "
                    "are absent (e.g. ``no source/parent run id`` / ``no plan "
                    "artifact`` / ``no delivery gate`` / ``no active child``). "
                    "Empty when a continuation subject was resolved.",
    )


# ── Provider-pressure vocabulary (shared by status / evidence / diagnose /
# summary) ───────────────────────────────────────────────────────────────────
#
# The single typed wire model for core-typed *provider pressure* — a provider
# runtime/access failure (rate-limit, transient runtime fault, access loss)
# that core classifies in ``ErrorsAndHalt.provider_runtime`` /
# ``ErrorsAndHalt.recovery`` rather than a generic code/test/review failure.
# Defined here, in the cross-domain module, so every surface
# (``read.RunStatus``, ``inspection.ErrorsHaltSliceRecord``,
# ``run_control.RunDiagnosis``, ``observe.RunEventsSummary``) references one
# source of truth without importing from each other. It is built ONCE from
# ``services.run_projection.project_provider_pressure`` +
# ``build_provider_pressure_next_actions`` so the surfaces never drift.


class ProviderPressure(BaseModel):
    """Core-typed provider-pressure condition projected for MCP clients.

    A run is under *provider pressure* when core attaches a typed
    ``ProviderRuntimeFailure`` (``provider_runtime``) or
    ``ProviderAccessRecovery`` (``provider_access``) to its errors/halt
    rollup — a rate-limit, a transient provider runtime fault, or a loss of
    provider access — as opposed to a generic code/test/review failure. The
    classification comes ONLY from the core-typed source; MCP never derives it
    by parsing raw provider output or logs.

    Every field is additive-optional so a surface that has nothing to report
    omits it cleanly (``None`` / ``False`` / empty). ``next_actions`` are
    conservative and never imply a passed review/delivery, and never ask the
    operator to invent retry feedback.
    """

    condition: Literal["provider_pressure"] = Field(
        default="provider_pressure",
        description=(
            "Fixed marker identifying this as the typed provider-pressure "
            "condition. Always ``provider_pressure`` when the model is "
            "present."
        ),
    )
    failure_kind: str | None = Field(
        default=None,
        description=(
            "Core's typed failure kind — ``provider_runtime`` for a runtime "
            "fault/rate-limit, ``provider_access`` for a loss of access. "
            "``None`` when core attached no typed provider failure."
        ),
    )
    recoverable: bool = Field(
        default=False,
        description=(
            "True when core marks the provider failure as recoverable (the "
            "interrupted phase can be retried/resumed). False for an "
            "exhausted/terminal provider failure."
        ),
    )
    phase: str | None = Field(
        default=None,
        description="The phase that was interrupted by the provider failure "
                    "(core's ``failed_phase``); ``None`` when absent.",
    )
    pressure_kind: str | None = Field(
        default=None,
        description=(
            "Finer provider-pressure classification (future core field, e.g. "
            "rate-limit vs overload). ``None`` until core emits it; never "
            "fabricated by MCP."
        ),
    )
    retry_state: str | None = Field(
        default=None,
        description=(
            "Core's retry lifecycle for the provider failure (future field, "
            "e.g. ``parked_until_reset`` / ``exhausted``). ``None`` until "
            "core emits it."
        ),
    )
    reset_at: str | None = Field(
        default=None,
        description=(
            "When the provider reset window passes (future core field). "
            "``None`` until core emits it — MCP never fabricates a reset "
            "time."
        ),
    )
    wait_hint: str | None = Field(
        default=None,
        description=(
            "Human-readable wait guidance until the provider recovers (future "
            "core field). ``None`` until core emits it."
        ),
    )
    sanitized_message: str | None = Field(
        default=None,
        description=(
            "Core's sanitized provider message (``provider_message``) — never "
            "raw provider output. ``None`` when core left it empty."
        ),
    )
    recommended_action: str | None = Field(
        default=None,
        description=(
            "Core's recommended action for this provider failure "
            "(``resume_or_retry_phase`` for a runtime fault, "
            "``switch_runtime_or_restore_access`` for an access loss). "
            "``None`` when absent."
        ),
    )
    next_actions: list[NextActionRecord] = Field(
        default_factory=list,
        description=(
            "Conservative, typed follow-up calls built by the single shared "
            "``build_provider_pressure_next_actions`` helper. They inspect "
            "the provider-pressure evidence and resume/retry the interrupted "
            "phase (or wait for a reset window) — never imply a passed "
            "review/delivery and never require invented operator feedback."
        ),
    )


__all__ = [
    "ContinuationSubjectLiteral",
    "NextActionRecord",
    "ProviderPressure",
    "RecommendedNextActionLiteral",
    "RecoveryLineage",
]
