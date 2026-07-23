"""orcho_mcp.schemas.workspace — wire models for workspace tools.

Covers ``orcho_workspace_info`` (instance discovery),
``orcho_workspace_state`` (advisory cross-run state cache), and
``orcho_workspace_pending_decisions`` (artifact-built recovery of runs
paused awaiting an operator phase-handoff decision). All three are
read-only surfaces; none of these models carries raw event payloads,
prompts, findings, reviewer output, env, or credentials by design.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from orcho_mcp.schemas.shared import NextActionRecord


class WorkspaceInfo(BaseModel):
    """Where this orcho instance reads/writes runs.

    ``workspace_dir`` is None when the server can't resolve a workspace —
    typical when MCP started outside any orcho-tracked project. Tools that
    require workspace context surface ``WorkspaceNotResolvedError`` instead.
    """
    workspace_dir: str | None
    runs_dir: str | None
    recent_projects: list[str] = Field(
        default_factory=list,
        description="Distinct project paths seen across the most recent runs.",
    )


class WorkspaceRunStateRecord(BaseModel):
    """One run's last observed cursor / status / phase.

    Advisory — these fields are populated from the same data that
    ``orcho_run_status`` and ``orcho_run_events_summary`` return, and
    are updated by the MCP server only when a tool call walks the run.
    The authoritative state remains in the run's ``events.jsonl`` +
    ``meta.json`` artifacts; this record is a cache hint for reconnect.
    """

    run_id: str
    last_seq: int = Field(
        description="Highest ``next_seq`` observed by an MCP tool call. "
                    "Monotonic — never moves backward.",
    )
    last_status: str | None = Field(
        default=None,
        description="Status seen at the last observation. May lag the "
                    "authoritative meta + supervisor merge by one poll.",
    )
    last_phase: str | None = Field(
        default=None,
        description="Phase active at the last observation. ``None`` "
                    "when no phase is open.",
    )
    last_summary_at: str = Field(
        description="UTC ISO 8601 timestamp of the most recent update "
                    "to this record.",
    )


class WorkspaceMcpStateResult(BaseModel):
    """Advisory MCP workspace state envelope.

    This is *not* source of truth — it records what the MCP layer has
    most recently observed across all runs in the workspace. Missing or
    corrupt state files surface as a fresh empty envelope so callers can
    always rely on the shape.

    Contains no raw event payloads, prompts, findings, env, or
    credentials by design.
    """

    version: int = Field(
        description="Schema version. Bump only on shape changes — "
                    "readers tolerate unknown versions by "
                    "returning an empty envelope.",
    )
    workspace_dir: str
    server_started_at: str = Field(
        description="UTC ISO 8601 timestamp captured when *this* MCP "
                    "server process started.",
    )
    updated_at: str = Field(
        description="UTC ISO 8601 timestamp of the most recent write.",
    )
    runs: dict[str, WorkspaceRunStateRecord] = Field(
        default_factory=dict,
        description="Per-run last-observed records keyed by ``run_id``.",
    )


class WorkspacePendingDecisionRow(BaseModel):
    """One run paused awaiting an operator phase-handoff decision.

    Built from the run's durable artifacts (``meta.json`` +
    ``phase_handoff_decisions/``) via the shared ``project_pending_handoff``
    projection — never from the advisory ``mcp/state.json`` cache. Every
    field is already bounded: there are no raw findings, reviewer output,
    event payloads, or logs on this row by design.

    ``next_actions`` carries the typed, decision-coherent next step:

    - no decision recorded yet ⇒ a single ``operator_input_required``
      ``orcho_phase_handoff_decide`` record whose ``choices`` are the
      ``available_actions`` (the operator still has to pick a verb and,
      where required, supply feedback);
    - a decision artifact already exists ⇒ a single ``ready_call``
      ``orcho_run_resume`` record (the decision is recorded; the run only
      needs resume to continue), matching the resume routing the diagnose /
      live-status / summary surfaces already use.
    """

    run_id: str = Field(description="The paused run's id (its run-dir name).")
    classification: str = Field(
        default="actionable",
        description="Durable classification of this row: ``actionable`` "
                    "(project recorded, path exists, and lies under the "
                    "resolved workspace scope; for a standard "
                    "``workspace-orchestrator`` layout this includes the "
                    "parent project group), ``missing_project`` (no project "
                    "recorded or its path no longer exists), "
                    "``temp_project`` (project path lives in a temp/scratch "
                    "location), or ``out_of_workspace`` (project path exists "
                    "but is outside the resolved workspace scope). Default "
                    "rows are always ``actionable``; the forensic "
                    "``include_stale`` mode sets the real classification "
                    "reason on every returned row.",
    )
    project: str | None = Field(
        default=None,
        description="The run's project path from ``meta.project``; ``None`` "
                    "when the run did not record one.",
    )
    handoff_id: str | None = Field(
        default=None,
        description="Active handoff id from ``meta.phase_handoff.id``; "
                    "``None`` when the paused run did not record one.",
    )
    phase: str | None = Field(
        default=None,
        description="Phase that issued the pending handoff, when known.",
    )
    trigger: str | None = Field(
        default=None,
        description="Normalised pause trigger (e.g. ``rejected`` / "
                    "``incomplete``), when recorded.",
    )
    verdict: str | None = Field(
        default=None,
        description="Runtime machine verdict label (``REJECTED`` / "
                    "``APPROVED``) carried verbatim, when present.",
    )
    round_label: str | None = Field(
        default=None,
        description="Coherent operator round label (e.g. ``validate_plan "
                    "automatic round 1/1``); ``None`` when round counters "
                    "are absent.",
    )
    available_actions: list[str] = Field(
        default_factory=list,
        description="Decision verbs the runtime published for this handoff "
                    "(``continue`` / ``retry_feedback`` / ``halt`` / "
                    "``continue_with_waiver``), verbatim.",
    )
    decision_artifact_exists: bool = Field(
        default=False,
        description="``True`` when a phase-handoff decision artifact already "
                    "exists for ``handoff_id`` — the run stays paused, so the "
                    "next step is resume, not a second decide.",
    )
    decision_state: str = Field(default="missing", description="Typed decision-artifact outcome: recorded, missing, or degraded.")
    decision_degraded_reason: str | None = Field(default=None, description="Stable reason for a degraded decision read.")
    suggested_next_action: str | None = Field(
        default=None,
        description="One-line pointer at the right next tool "
                    "(decide-then-resume when no decision exists, resume "
                    "when one already does).",
    )
    next_actions: list[NextActionRecord] = Field(
        default_factory=list,
        description="Typed follow-up calls for this row — a ready "
                    "``orcho_run_resume`` when a decision is recorded, else "
                    "an ``operator_input_required`` "
                    "``orcho_phase_handoff_decide`` carrying the available "
                    "verbs in ``choices``.",
    )


class WorkspacePendingDecisionsResult(BaseModel):
    """Returned by ``orcho_workspace_pending_decisions``.

    A bounded, artifact-built index of every run currently paused on
    ``status=awaiting_phase_handoff`` across the workspace — the typed way
    for a captain to recover pending operator decisions without chat
    memory. Rows are derived from each run's ``meta.json`` /
    ``phase_handoff_decisions/`` artifacts (NOT the advisory
    ``mcp/state.json`` cache) and are capped; ``truncated`` / ``scanned_count``
    expose the bound so a silent cap is never read as "nothing more pending".

    By default only ``actionable`` rows are returned; non-actionable rows
    (missing project, temp/scratch project path, or a project outside the
    workspace scope) are hidden but never silently dropped — they are tallied
    in ``hidden_count`` and its per-reason breakdown. The standard
    ``workspace-orchestrator`` scope includes its parent project group so
    sibling project repos remain actionable. The breakdown always sums to
    ``hidden_count``. These counters are computed by ``classification`` over
    the scan window only (bounded by the same internal ceiling as
    ``scanned_count``), so they are a window-local tally, not a full global
    count of every non-actionable run that could exist. The counters are
    computed identically whether or not ``include_stale`` returned the hidden
    rows: turning ``include_stale`` on surfaces the same rows the counters
    already accounted for, with their real ``classification`` set, and does
    not change any counter value for the same set of scanned runs.
    """

    runs: list[WorkspacePendingDecisionRow] = Field(
        default_factory=list,
        description="Paused-run rows, newest run id first. Length is capped; "
                    "see ``truncated``. By default only ``actionable`` rows "
                    "appear; with ``include_stale`` the hidden non-actionable "
                    "rows are appended too, each carrying its real "
                    "``classification``.",
    )
    scanned_count: int = Field(
        default=0,
        description="Number of run directories examined during the scan "
                    "(bounded by an internal ceiling).",
    )
    returned_count: int = Field(
        default=0,
        description="Number of rows returned (``len(runs)``).",
    )
    truncated: bool = Field(
        default=False,
        description="``True`` when the result was capped — either the row "
                    "limit was reached or the scan ceiling stopped "
                    "enumeration before the runs directory was exhausted. "
                    "More paused runs may exist beyond what is shown.",
    )
    hidden_count: int = Field(
        default=0,
        description="Number of non-actionable rows within the scan window "
                    "that the default view hides. Computed by "
                    "``classification`` and bounded by the scan ceiling, so "
                    "it is a window-local tally rather than a full global "
                    "count. Equals the sum of the three ``hidden_*_count`` "
                    "fields below. Independent of ``include_stale``: the same "
                    "value holds whether or not the hidden rows are returned. "
                    "These rows are never silently discarded — they are "
                    "counted here and, with ``include_stale``, also returned.",
    )
    hidden_missing_project_count: int = Field(
        default=0,
        description="Hidden rows classified ``missing_project`` (no project "
                    "recorded or its path no longer exists). Part of the "
                    "``hidden_count`` breakdown; window-bounded and "
                    "independent of ``include_stale``.",
    )
    hidden_temp_project_count: int = Field(
        default=0,
        description="Hidden rows classified ``temp_project`` (project path "
                    "lives in a temp/scratch location). Part of the "
                    "``hidden_count`` breakdown; window-bounded and "
                    "independent of ``include_stale``.",
    )
    hidden_out_of_workspace_count: int = Field(
        default=0,
        description="Hidden rows classified ``out_of_workspace`` (project "
                    "path exists but is outside the resolved workspace "
                    "scope). "
                    "Part of the ``hidden_count`` breakdown; window-bounded "
                    "and independent of ``include_stale``.",
    )


__all__ = [
    "WorkspaceInfo",
    "WorkspaceMcpStateResult",
    "WorkspacePendingDecisionRow",
    "WorkspacePendingDecisionsResult",
    "WorkspaceRunStateRecord",
]
