"""orcho_mcp.tools — MCP tool registration / adapter layer.

Every ``@mcp.tool`` handler lives here so FastMCP sees stable names,
signatures, docstrings, and JSON Schemas as the wire contract. The
handler bodies are intentionally trivial — one-line delegations into
the orcho_mcp domain modules where the real implementation lives:

  ``orcho_mcp.services``       — read-only SDK adapters (run lookup,
                                  history, workspace info, profiles,
                                  skills, status / metrics / events,
                                  status merge).
  ``orcho_mcp.observe``        — bounded events summary, long-poll
                                  watch, paused-run handoff hint,
                                  advisory state observation.
  ``orcho_mcp.run_control``    — start / resume / cancel + phase
                                  handoff / delivery decisions.
  ``orcho_mcp.inspection``     — evidence slices + run diff read.
  ``orcho_mcp.authoring``      — plan validation + prompt resolution.

This module deliberately holds **no** business logic, **no** SDK calls,
**no** file IO. The architecture gate
``test_tools_py_stays_wire_adapter`` enforces that invariant by
forbidding imports of ``sdk``, ``core.observability.events``, and
``pipeline.plan_parser`` here. Any future read tool should add its
implementation to the matching domain module and keep tools.py as a
thin shim.

All inputs are plain typed args (FastMCP infers JSON Schema from hints).
All outputs are ``orcho_mcp.schemas`` Pydantic models so the wire shape
is one place, easy to round-trip in tests.

⚠️ stdio purity invariant: never ``print()`` from a handler. Errors
raise ``orcho_mcp.errors`` subclasses; FastMCP turns them into JSON-RPC
errors.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import Context

from orcho_mcp.authoring.plan_validation import validate_plan_document
from orcho_mcp.authoring.prompt_resolution import resolve_prompt

# Profile discovery reads the canonical v2 catalogue
# (``pipeline_profiles_v2.json``) only.
from orcho_mcp.inspection.diagnosis import inspect_run_diagnosis
from orcho_mcp.inspection.diff import inspect_run_diff
from orcho_mcp.inspection.evidence import inspect_run_evidence
from orcho_mcp.instance import mcp
from orcho_mcp.observe.live_status import build_run_live_status
from orcho_mcp.observe.summary import build_run_events_summary
from orcho_mcp.observe.watch import watch_run
from orcho_mcp.run_control.advice import request_advice
from orcho_mcp.run_control.delivery import decide_delivery
from orcho_mcp.run_control.handoff import decide_phase_handoff_with_elicitation
from orcho_mcp.run_control.lifecycle import (
    cancel_run,
    resume_run,
    start_run,
)
from orcho_mcp.run_control.typed_pilot import (
    run_project_typed_silent,
    start_project_typed_silent_async,
)
from orcho_mcp.schemas import (
    CancelResult,
    DeliveryDecideResult,
    DeliveryGateProjection,
    EventsTailResult,
    EvidenceResult,
    HandoffAdviceResult,
    HistoryResult,
    PhaseHandoffDecideResult,
    PlanValidateResult,
    ProfilesListResult,
    PromptResolveResult,
    ResumeBlockedResult,
    ResumePendingDecisionResult,
    RunDiagnosis,
    RunDiffResult,
    RunEventsSummary,
    RunLiveStatusCard,
    RunMetrics,
    RunResumeResult,
    RunStartedResult,
    RunStatus,
    RuntimeOverrideArg,
    RunWatchResult,
    SkillsListResult,
    TypedRunResult,
    TypedRunStartedResult,
    WorkflowRecipeList,
    WorkspaceInfo,
    WorkspaceMcpStateResult,
    WorkspacePendingDecisionsResult,
)
from orcho_mcp.services.delivery_gate import project_delivery_gate
from orcho_mcp.services.pending_decisions import project_pending_decisions
from orcho_mcp.services.read_queries import (
    get_profiles_list,
    get_project_skills,
    get_run_history,
    get_workspace_info,
)
from orcho_mcp.services.run_lookup import find_run_dir, runs_dir_or_raise
from orcho_mcp.services.run_reads import (
    get_run_events_tail,
    get_run_metrics,
    get_run_status,
    get_workspace_mcp_state,
)
from orcho_mcp.services.workflow_recipes import list_workflow_recipes

logger = logging.getLogger(__name__)


# ── SDK adapters ─────────────────────────────────────────────────────────────

def _runs_dir_or_raise() -> Path:
    """Deprecated — use :func:`orcho_mcp.services.run_lookup.runs_dir_or_raise`.

    Retained as a thin delegating wrapper so external callers and any
    downstream that imported the old underscore name keep working
    through one release of leeway.
    """
    return runs_dir_or_raise()


def _find_run_dir(run_id: str) -> Path:
    """Deprecated — use :func:`orcho_mcp.services.run_lookup.find_run_dir`.

    Retained as a thin delegating wrapper; production callsites in this
    module now call the service entry point directly.
    """
    return find_run_dir(run_id)


# ── orcho_workspace_info ─────────────────────────────────────────────────────

@mcp.tool()
def orcho_workspace_info() -> WorkspaceInfo:
    """Return where orcho reads/writes runs and which projects appear in recent history.

    Useful as an initial discovery call so the client knows whether other
    tools that need ``project_dir`` will succeed.
    """
    return get_workspace_info()


# ── orcho_workspace_state ───────────────────────────────────────────────────


@mcp.tool()
def orcho_workspace_state() -> WorkspaceMcpStateResult:
    """Return advisory MCP workspace state — last observed cursor per run.

    Reads ``<ORCHO_WORKSPACE>/mcp/state.json`` (written by every
    ``orcho_run_events_summary`` and ``orcho_run_watch`` call). Useful
    for reconnect after a client/session restart: instead of replaying
    from ``since_seq=0``, callers pick the run's ``last_seq`` and pass
    it back as ``since_seq``.

    **Advisory only.** The canonical truth is each run's ``events.jsonl``
    + ``meta.json``. A missing or corrupt state file is recreated as an
    empty envelope on the next observation, so this tool never fails on
    that account.

    The state file contains no raw event payloads, prompts, findings,
    env, or credentials — it is deliberately a small reconnect index.

    Errors:
        - WorkspaceNotResolvedError — no $ORCHO_WORKSPACE / $ORCHO_WORKTREE.
    """
    return get_workspace_mcp_state()


# ── orcho_workspace_pending_decisions ────────────────────────────────────────


@mcp.tool()
def orcho_workspace_pending_decisions(
    limit: int | None = None,
    include_stale: bool = False,
) -> WorkspacePendingDecisionsResult:
    """List the runs awaiting an operator phase-handoff decision.

    The typed way for a captain to recover pending operator decisions
    *without chat memory*: it scans the workspace runs directory for runs on
    ``status=awaiting_phase_handoff`` and returns one bounded row per run.

    **Default view = the operator's decision inbox.** By default this answers
    "what needs my decision *now*", not "what historical artifact is paused
    somewhere". It returns only ``actionable`` rows — runs whose recorded
    ``meta.project`` path still exists *and* lives under the resolved
    workspace scope. In the standard ``workspace-orchestrator`` layout this
    scope includes both the orchestrator directory and its parent project
    group, where sibling project repos live. Non-actionable paused runs are
    hidden but never silently dropped: each is tallied in ``hidden_count`` and
    its per-reason breakdown (``hidden_missing_project_count`` for runs with
    no/missing project, ``hidden_temp_project_count`` for temp/scratch project
    paths, ``hidden_out_of_workspace_count`` for projects outside the
    workspace scope).

    **Workspace-valid beats temp.** A run whose project exists under the
    workspace scope is ``actionable`` even when that scope itself lives in a
    temp/test directory (e.g. ``/private/var/folders`` or under a ``pytest-*``
    path). The temp/scratch heuristic is only applied *after* that check, so a
    legitimate workspace launched from a temporary directory is never
    mis-hidden as a throwaway demo run.

    **Counters are bounded and view-independent.** ``hidden_count`` and its
    breakdown are computed by classification over the scan window only
    (bounded by the same internal run-directory ceiling as ``scanned_count``),
    so they are a window-local tally, not a global count of every
    non-actionable run that could exist. They are computed identically whether
    or not ``include_stale`` returns the hidden rows: flipping
    ``include_stale`` changes which rows appear, never the counter values for
    the same set of scanned runs. The breakdown always sums to
    ``hidden_count``.

    **Forensic escape hatch.** Pass ``include_stale=True`` to also return the
    hidden rows — every paused run, not just the actionable inbox. In that
    mode each returned row carries its real ``classification``
    (``actionable`` | ``missing_project`` | ``temp_project`` |
    ``out_of_workspace``) so you can see *why* a run would otherwise be
    hidden. Default rows are always ``actionable``.

    **Backward compatible.** Existing ``limit``-only callers keep working
    unchanged: the signature still accepts ``limit`` and ``include_stale`` is
    a new optional argument defaulting to ``False`` (the actionable inbox).

    **Artifact projector, not advisory.** Every row is built from the run's
    durable artifacts — ``meta.json`` (merged with supervisor truth) plus the
    ``phase_handoff_decisions/`` directory — NOT from the advisory
    ``mcp/state.json`` cache that ``orcho_workspace_state`` reads. A run shows
    up here iff its on-disk state says it is paused.

    Each row carries the compact handoff read-model (handoff_id / phase /
    trigger / verdict / round_label / available_actions /
    decision_artifact_exists / suggested_next_action) and a typed
    ``next_actions`` with the exact next-step form, coherent with
    ``orcho_run_diagnose`` and the resume pre-flight guard:

    - no decision recorded yet ⇒ an ``operator_input_required``
      ``orcho_phase_handoff_decide`` whose ``choices`` are the
      ``available_actions``;
    - a decision artifact already exists ⇒ a ready ``orcho_run_resume`` call
      (the run stays paused; it only needs resume to continue).

    Rows never contain raw findings, reviewer output, event payloads, or
    logs. The result is bounded: the scan stops at an internal run-directory
    ceiling and the visible rows are capped by ``limit`` (default 50);
    ``truncated`` and ``scanned_count`` expose the bound so a cap is never
    mistaken for "nothing more pending". A corrupt / unreadable single run is
    skipped, not fatal.

    Args:
        limit: optional cap on the number of rows returned (default 50,
            clamped to the scan ceiling). Newest runs are returned first.
        include_stale: when ``False`` (default) return only the actionable
            inbox and hide non-actionable rows (still counted in
            ``hidden_*``); when ``True`` also return the hidden rows, each
            stamped with its real ``classification``.

    Errors:
        - WorkspaceNotResolvedError — no $ORCHO_WORKSPACE / $ORCHO_WORKTREE.
    """
    return project_pending_decisions(limit=limit, include_stale=include_stale)


# ── orcho_run_history ────────────────────────────────────────────────────────────

@mcp.tool()
def orcho_run_history(limit: int = 10, project_dir: str | None = None) -> HistoryResult:
    """List the most recent runs, newest first.

    Args:
        limit: max number of runs to return (default 10).
        project_dir: optional filter — only include runs whose meta.json
            ``project`` field equals this path. Path is compared as-is (no
            resolution); pass an absolute path to match how orcho records it.
    """
    return get_run_history(limit=limit, project_dir=project_dir)


# ── orcho_run_status ─────────────────────────────────────────────────────────────

@mcp.tool()
def orcho_run_status(
    run_id: str,
    include: list[str] | None = None,
) -> RunStatus:
    """Summary snapshot for a single run — status, metrics, lineage, next steps.

    Raises RunNotFoundError if ``run_id`` doesn't exist on disk.

    Merges supervisor truth into ``meta.status`` when the pipeline exits
    before it can update ``meta.json``. The pipeline owns ``meta.json``;
    the supervisor owns ``mcp_supervisor.json``. When meta status is
    missing or stuck on ``running`` while the supervisor knows the
    process is dead, this tool surfaces the supervisor's terminal
    status so wire consumers see one consistent value.

    **Summary-only by default.** This tool is the supervisor's
    highest-frequency poll, so ``meta`` is projected to a bounded shape:
    top-level scalars and gate verdicts pass through, but the heavy phase
    *bodies* are elided. Inside ``meta.phases`` you get size / count
    markers instead of full text — e.g. ``plan[i].output_chars`` (not the
    plan markdown), ``implement.output_chars``, ``validate_plan[i]``
    keeps ``verdict`` + ``findings_count`` (not the critique), and
    implementation receipts collapse to ``subtask_id`` + ``state``. The
    long task text remains in ``meta.task`` but is truncated there, with
    ``task_chars`` / ``task_truncated`` markers. Per-attempt observability dicts
    (``prompt_render``, ``context_*``) are dropped.

    The full bodies are never lost — they live on disk under ``run_dir``
    and are reachable via ``orcho_run_evidence`` (plan / findings /
    receipts slices), ``orcho_run_metrics`` (per-phase token/duration
    breakdown), or a direct read of ``runspace/runs/<run_id>/``.

    ``auto_detect`` is a typed projection of the run's profile-selector
    decision, present only when the run started via
    ``profile="auto-detect"`` (else ``None``). It carries
    ``requested_selector`` (the ``auto-detect`` token), the selected /
    recommended profile + mode, ``detection_state`` / ``disposition`` /
    ``trusted``, and a deterministic ``next_action``: ``None`` for a
    trusted recommendation, otherwise an ``operator_input_required``
    pointer to re-run with an explicit profile.

    Args:
        run_id: the run to inspect.
        include: opt back in to full phase bodies. Tokens — ``"task"``,
            ``"plan"`` (plan markdown + file lists), ``"output"`` (implement
            agent output), ``"critiques"`` (validate_plan / repair /
            final_acceptance critiques + findings), ``"receipts"`` (full
            implementation / repair receipts), ``"all"`` (identity — the
            pre-summary payload, for callers that relied on it).
            Unrecognised tokens are ignored. Default ``None`` = summary.
    """
    return get_run_status(run_id, include=include)


# ── orcho_run_metrics ────────────────────────────────────────────────────────────

@mcp.tool()
def orcho_run_metrics(run_id: str) -> RunMetrics:
    """Raw metrics.json for a single run (token counts, durations, per-phase breakdown)."""
    return get_run_metrics(run_id)


# ── orcho_run_events_tail ────────────────────────────────────────────────────────

@mcp.tool()
def orcho_run_events_tail(
    run_id: str,
    since_seq: int = 0,
    limit: int = 25,
) -> EventsTailResult:
    """Return events with seq > since_seq, newest events at the end.

    Designed for catch-up after a client reconnect: client persists the
    last-seen seq, calls this tool with ``since_seq=last_seen`` and gets
    everything missed since. ``next_seq`` is the highest seq returned (or
    ``since_seq`` if nothing new). ``eof=True`` means no events remain
    after ``next_seq`` at this snapshot — caller can stop polling until
    the run state changes.

    Args:
        run_id: the run to tail.
        since_seq: only return events with seq strictly greater. Default 0
            returns all events from the start.
        limit: cap on returned events; default 25. Caller paginates by
            advancing ``since_seq`` to the previous ``next_seq``.
    """
    return get_run_events_tail(run_id, since_seq=since_seq, limit=limit)


# ── orcho_run_events_summary ────────────────────────────────────────────────


@mcp.tool()
def orcho_run_events_summary(
    run_id: str,
    since_seq: int = 0,
    limit: int = 50,
    last_n: int = 5,
) -> RunEventsSummary:
    """Return a bounded summary of a run's recent events.

    The agent's typical question while a run is in flight is "what
    changed and what should I do next?" — not "give me every raw
    event." This tool answers the first question in a single bounded
    payload, so status polling no longer triggers MCP tool-result
    auto-spill on the client side.

    This tool is also the fallback half of the resilient observation
    loop ``orcho_run_watch`` → ``orcho_run_events_summary`` →
    ``orcho_run_watch``. When a bounded ``orcho_run_watch`` times out or
    the client transport drops the long-poll, call this with
    ``since_seq`` set to the watch's ``trigger.seq`` /
    ``summary.next_seq`` to catch up on what happened while
    disconnected, then resume watching from this summary's ``next_seq``.
    A disconnected watch is not a failed run — the run keeps executing,
    and decisions are taken from ``status`` / ``pending_handoff`` /
    terminal signals here, never from the watch call ending.

    The wire shape is `RunEventsSummary`. Counts, ``by_phase``,
    ``by_kind``, and ``last_n`` are computed over the ``(since_seq,
    limit)`` window; ``current_phase`` is computed over the *full*
    event stream up to ``next_seq`` so polling-style callers don't
    lose phase context when the window misses the original
    ``phase.start``. ``status`` mirrors what ``orcho_run_status``
    returns (meta + supervisor merge). ``next_actions`` are short
    imperative strings derived conservatively from status only.

    Args:
        run_id: the run to summarise.
        since_seq: only consider events with seq > since_seq. Default
            ``0`` includes everything from the start. Must be ≥ 0.
        limit: cap on events considered in this batch. Default 50,
            hard ceiling 1000. ``next_seq`` advances by at most
            ``limit`` events per call.
        last_n: number of compact events to include in the tail.
            Default 5, hard ceiling 100. ``0`` returns an empty list.

    Errors:
      - RunNotFoundError — unknown run_id.
      - WorkspaceNotResolvedError — no $ORCHO_WORKSPACE / $ORCHO_WORKTREE.
      - InvalidPlanError — input out of range.

    For raw event dumps (forensic replay, exact payload inspection)
    keep using ``orcho_run_events_tail``; this tool deliberately drops
    payload bodies.
    """
    return build_run_events_summary(
        run_id, since_seq=since_seq, limit=limit, last_n=last_n,
    )


# ── orcho_run_live_status ───────────────────────────────────────────────────


@mcp.tool()
def orcho_run_live_status(run_id: str) -> RunLiveStatusCard:
    """Return a bounded operator-safe live status card for a mono run.

    One typed snapshot that answers "where is this run right now, and
    what should I do?" in a single bounded payload — built for
    high-frequency polling, with every embedded preview truncated and no
    full phase bodies, critiques, or raw logs ever riding along. It
    unites the durable meta status (with supervisor terminal fallback),
    the live phase/subtask position, the last significant activity, any
    pending phase-handoff, and terminal consistency — without scraping
    raw event logs.

    Branch on ``state_class`` (a closed set):

    - ``running_phase`` — executing a phase, no subtask in flight
      (``current_phase`` set).
    - ``running_subtask`` — executing a ``subtask_dag`` subtask
      (``current_subtask`` carries index/total/goal/state).
    - ``awaiting_handoff`` — paused on a phase-handoff decision
      (``pending_handoff`` carries handoff id / phase / available actions
      / default action / verdict / findings summary / recommended action).
    - ``terminal_success`` — a clean terminal success
      (``terminal.resume_meaningful`` is ``False`` — resume is inert).
    - ``terminal_halted`` — a halted / failed / interrupted terminal
      (``terminal.halt_reason`` set; ``resume_meaningful`` ``True``).
    - ``terminal_inconsistent`` — a terminal success whose
      ``final_acceptance`` contradicts it (e.g. ``done`` + ``REJECTED``);
      the contradiction is surfaced in ``consistency_flags``, never hidden.

    ``next_seq`` is the latest event seq — carry it into
    ``orcho_run_watch`` / ``orcho_run_events_summary`` to resume
    observation from here.

    Args:
        run_id: the run to inspect.

    Errors:
      - RunNotFoundError — unknown run_id.
      - WorkspaceNotResolvedError — no $ORCHO_WORKSPACE / $ORCHO_WORKTREE.

    For the windowed event aggregate use ``orcho_run_events_summary``;
    for raw payload replay use ``orcho_run_events_tail``.
    """
    return build_run_live_status(run_id)


# ── orcho_run_watch ─────────────────────────────────────────────────────────


@mcp.tool()
async def orcho_run_watch(
    run_id: str,
    since_seq: int = 0,
    until: Literal[
        "next_event",
        "phase_change",
        "subtask",
        "handoff_or_terminal",
        "terminal",
    ] = "handoff_or_terminal",
    timeout_s: int = 3600,
    summary: bool = True,
    interaction_client: str = "generic",
    ctx: Context | None = None,
) -> RunWatchResult:
    """Long-poll a run until something meaningful happens.

    Holds the MCP request open until the chosen ``until`` condition fires
    or ``timeout_s`` expires. Designed to replace manual re-polling of
    ``orcho_run_status`` / ``orcho_run_events_summary`` during long
    phases (implement, repair) where minutes can pass between
    user-relevant events. When the MCP request carries a
    ``progressToken``, ordered ``notifications/progress`` are emitted as
    the event sequence advances (one notification per observed seq
    advance, not per individual event) so clients can show live status
    without the operator-side LLM having to round-trip.

    Reconnect rule: pass ``result.summary.next_seq`` (or
    ``result.trigger.seq`` when ``summary=False``) as the next
    ``since_seq``. Both fields carry the same value at return time.

    Resilient observation: clients whose transport caps a single
    tool-call's duration should pass a short bounded ``timeout_s``
    (120–240s is a convenient window) rather than the 3600s default, so
    each watch returns promptly with a fresh reconnect cursor. On a
    ``timeout`` trigger — or if the transport drops the long-poll
    mid-call — fall back to
    ``orcho_run_events_summary(since_seq=trigger.seq)`` to catch up, then
    resume ``orcho_run_watch`` from that summary's ``next_seq``. The loop
    is ``orcho_run_watch`` → ``orcho_run_events_summary`` →
    ``orcho_run_watch``. A disconnected watch is not a failed run: the run
    keeps executing in its worktree, and lifecycle decisions are read only
    from the typed ``status`` / ``handoff`` / terminal / evidence signals,
    never from a watch call ending early.

    Args:
        run_id: the run to watch.
        since_seq: reconnect cursor. Defaults to ``0`` (watch from the
            start of the event stream). Must be ≥ 0.
        until: trigger condition.
            ``"next_event"`` — any event with seq > since_seq.
            ``"phase_change"`` — phase transitions (including end-to-None),
            or handoff/terminal.
            ``"subtask"`` — each subtask_dag boundary (a subtask starts or
            ends) during a long implement phase, or handoff/terminal. The
            result's ``summary.current_subtask`` carries the live
            index/total/goal/state, and progressToken notifications read
            "implement: subtask 3/12 done (<goal>)".
            ``"handoff_or_terminal"`` — run pauses
            (``awaiting_phase_handoff`` / ``awaiting_gate_decision``) or
            ends.
            ``"terminal"`` — run reaches a terminal status.
        timeout_s: max seconds to hold the request open. Default 3600 (1h);
            hard ceiling 7200 (2h). Must be in (0, 7200].
        summary: when True, include a bounded ``RunEventsSummary`` in the
            result. When False, ``result.summary`` is ``None`` and the
            reconnect cursor lives on ``result.trigger.seq``.
        interaction_client: **presentation hint only** for the
            ``handoff.client_hints`` payload when the run pauses.
            First-class values: ``generic``, ``claude-code``, ``codex``.
            Unknown values normalise to ``generic`` so future clients
            stay forward-compatible. Does **not** affect decision
            correctness, ``available_actions``, ``default_action``,
            trigger kind, summary, or progress — the agent reads this to
            decide *how* to render the prompt, not *what* to do.

    Errors:
        - RunNotFoundError — unknown run_id.
        - WorkspaceNotResolvedError — no $ORCHO_WORKSPACE.
        - InvalidPlanError — input out of range, or unknown ``until``.

    Returns ``RunWatchResult``. On timeout, ``triggered=False`` and
    ``trigger.kind=="timeout"``; on any other path ``triggered=True``.
    Raw event payloads are never returned — use ``orcho_run_events_tail``
    for forensic replay.
    """
    return await watch_run(
        run_id,
        since_seq=since_seq,
        until=until,
        timeout_s=timeout_s,
        summary=summary,
        interaction_client=interaction_client,
        ctx=ctx,
    )


# ── orcho_plan_validate ──────────────────────────────────────────────────────

@mcp.tool()
def orcho_plan_validate(
    markdown: str | None = None,
    path: str | None = None,
) -> PlanValidateResult:
    """Validate an architect plan document. Provide exactly one of ``markdown`` or ``path``.

    Wraps ``pipeline.plan_parser.parse_plan`` — same semantics as the
    pipeline's own DECOMPOSE_QA gate. Returns ``ok=False`` with a
    human-readable error rather than raising for parse/DAG failures, so
    the LLM caller can read the error and try again.

    InvalidPlanError is raised only for invalid arguments (both sources or neither).
    """
    return validate_plan_document(markdown=markdown, path=path)


# ── orcho_skills_list ────────────────────────────────────────────────────────

@mcp.tool()
def orcho_skills_list(project_dir: str) -> SkillsListResult:
    """Discover Agent Skills packages visible to the project.

    Walks the canonical multi-source chain (project / compat / workspace
    / user / entry-point packages); returns a deterministic registry
    snapshot. Trust gating defaults to the safe policy
    (project + compat sources OFF until the user opts in via plugin
    config); MCP introspection is read-only and per-call.

    Returns an empty list when no source yields a SKILL.md — that's the
    'flat developer-agent mode' fallback, not an error.
    """
    return get_project_skills(project_dir=project_dir)


# ── orcho_prompts_resolve ────────────────────────────────────────────────────

@mcp.tool()
def orcho_prompts_resolve(
    name: str,
    project_dir: str | None = None,
) -> PromptResolveResult:
    """Resolve a prompt template through the 3-level chain (project → workspace → core).

    Returns the chain (with exists flags) and the content of the winning
    entry. If ``project_dir`` is None, only the core level is consulted.
    """
    return resolve_prompt(name=name, project_dir=project_dir)


# ── orcho_profiles_list ──────────────────────────────────────────────────────

@mcp.tool()
def orcho_profiles_list() -> ProfilesListResult:
    """Return the catalogue of pipeline profiles.

    Reads the canonical v2 profile catalogue exclusively.

    Result shape:
      * ``source="json_v2"`` — v2 file loaded successfully;
        ``profiles`` carries the catalogue; ``diagnostic`` is None.
      * ``source="missing"`` — no v2 file at the resolved path;
        ``profiles=[]`` and ``diagnostic`` explains the cause +
        expected location. MCP clients SHOULD surface the
        diagnostic to the user (it's actionable: "install
        orcho-core ≥ 0.X" or "set ORCHO_PROFILES_V2_PATH").

    ``source="missing"`` means no profile catalogue was found at the
    resolved path; the diagnostic explains where the server looked and
    how to override the path.
    """
    return get_profiles_list()


# ── orcho_workflows_list ─────────────────────────────────────────────────────

@mcp.tool()
def orcho_workflows_list() -> WorkflowRecipeList:
    """Return the machine-readable catalogue of workflow recipes.

    Recipes describe how a sequence of MCP tool calls fits together
    (plan-then-implement, review a paused run, resume a crashed run,
    inspect a finished run). The server does not execute recipes —
    the client agent reads the steps and decides which tool to call
    next. Same payload is also published as the ``orcho://workflows``
    resource; this tool exists so tools-only clients (which cannot
    read MCP resources) can still discover the catalogue.

    The result is stable across calls: recipe order, step order, and
    step ``id`` values do not shuffle between invocations.
    """
    return list_workflow_recipes()


# ── Execute tools ────────────────────────────────────────────────────────────

@mcp.tool()
async def orcho_run_start(
    task: str | None = None,
    task_file: str | None = None,
    project_dir: str = ".",
    profile: str = "feature",
    mock: bool = False,
    max_rounds: int | None = None,
    mock_validate_plan_reject: int = 0,
    output_mode: Literal["summary", "live", "debug"] = "summary",
    session_mode: Literal["auto", "stateless", "chain", "hybrid"] = "auto",
    attach: list[str] | None = None,
    attach_text: list[str] | None = None,
    attach_image: list[str] | None = None,
    attach_binary: list[str] | None = None,
    from_run_plan: str | None = None,
) -> RunStartedResult:
    """Spawn an orcho pipeline run in the background; return run_id immediately.

    The run executes asynchronously in a detached subprocess. For live
    status, prefer ``orcho_run_watch`` — it holds the request open and
    returns the moment something meaningful changes (next event, phase
    change, handoff, or terminal), and emits ordered
    ``notifications/progress`` when the request carries a
    ``progressToken``. The response's ``next_actions`` already carries a
    ready ``ready_call`` to ``orcho_run_watch`` pre-filled with the new
    ``run_id``, so the client can enter that watch loop immediately
    without re-deriving the call. For one-shot polling,
    ``orcho_run_status``, ``orcho_run_events_summary``,
    ``orcho_run_metrics``, and ``orcho_run_events_tail`` (or the matching
    ``orcho://runs/{run_id}/...`` resources) all work without blocking.

    Provide either ``task`` (inline string) or ``task_file`` (markdown
    path). ``project_dir`` accepts an absolute path or a path relative
    to the MCP server's working directory (e.g. ``"orcho-web"`` from the
    workspace root, or ``"."`` for the current directory); it is resolved
    once at spawn time and the absolute result is used for both the
    subprocess cwd and the orchestrator's ``--project`` flag. A missing
    or non-directory path fails fast with ``PipelineSpawnError`` rather
    than dying inside the subprocess. ``mock=true`` runs the full
    pipeline flow with fake agents (no LLM tokens consumed) — recommended
    for first invocations.
    ``profile`` selects the active pipeline profile by semantic work
    kind (``feature``, ``small_task``, ``complex_feature``,
    ``planning``, ``code_review``, …); default ``"feature"``.
    ``profile="auto-detect"`` is a supported *selector* value (not an
    executable recipe): core classifies the task, picks the matching
    semantic work kind, and in this non-interactive surface proceeds
    with the chosen profile when confidence is sufficient (otherwise it
    falls back to the default profile). The decision is read back
    typed via ``orcho_run_status`` — the ``auto_detect`` projection
    carries ``requested_selector`` (the ``auto-detect`` token), the
    selected / recommended profile + mode, ``detection_state``, and a
    deterministic ``next_action``. ``orcho_profiles_list`` lists
    selectors separately from executable profiles under ``selectors``.

    ``output_mode`` controls subprocess transcript verbosity:
    ``"summary"`` (default), ``"live"``, or ``"debug"``.
    ``session_mode`` controls implement → repair_changes provider-session
    continuation: ``"auto"`` (default), ``"stateless"``, ``"chain"``, or
    ``"hybrid"``.

    Pause semantics are driven by each phase's declared ``handoff``
    policy in the active profile (e.g. ``feature`` declares
    ``human_feedback_on_reject`` on ``validate_plan``). When the
    pipeline pauses, ``meta.status`` becomes ``awaiting_phase_handoff``
    and the subprocess exits rc=4; the caller resolves the pause via
    ``orcho_phase_handoff_decide`` and follows up with
    ``orcho_run_resume`` for ``continue`` / ``retry_feedback`` /
    ``continue_with_waiver`` actions. ``halt`` is terminal and needs no
    resume.

    Attachments:
        ``attach``: paths whose kind is auto-detected from extension.
        ``attach_text`` / ``attach_image`` / ``attach_binary``: paths
        whose kind is forced. All four parameters are repeatable; each
        path becomes one ``Attachment`` threaded into the agent's
        prompt context. TEXT attachments inject as XML-style block;
        IMAGE / BINARY currently flow through ``state.attachments``.

    From-run-plan follow-up:
        ``from_run_plan`` (optional): parent run id or absolute path
        whose ``parsed_plan.json`` the new run inherits. When set:
          * loads the parent's parsed plan via the typed artefact
            loader — no markdown round-trip;
          * projects the selected ``profile`` to drop the leading
            plan + validate_plan block, so the child run starts at
            implement with ``state.parsed_plan`` already hydrated;
          * stamps ``plan_source="run"`` + ``plan_source_run_id``
            on the child's meta.json for child → parent correlation.
        The parent run must contain ``parsed_plan.json``; otherwise
        the call fails with an actionable diagnostic.
        Useful when an operator wants to iterate on implementation
        without re-running the planning phases against the same task.
        Distinct from ``orcho_run_resume`` — this spawns a NEW run
        that inherits a parent's plan, while ``orcho_run_resume``
        continues the SAME run from its checkpoint.
    """
    return await start_run(
        task=task,
        task_file=task_file,
        project_dir=project_dir,
        profile=profile,
        mock=mock,
        max_rounds=max_rounds,
        mock_validate_plan_reject=mock_validate_plan_reject,
        output_mode=output_mode,
        session_mode=session_mode,
        attach=attach,
        attach_text=attach_text,
        attach_image=attach_image,
        attach_binary=attach_binary,
        from_run_plan=from_run_plan,
    )


@mcp.tool()
def orcho_run_project_typed(
    task: str,
    project_dir: str,
    output_dir: str,
    profile: str = "task",
    mock: bool = True,
    max_rounds: int = 1,
) -> TypedRunResult:
    """Drive a single-project pipeline run as a foreground library call.

    Pilot tool that calls orcho-core's typed silent boundary
    (``run_project_pipeline`` with
    ``presentation=PresentationPolicy.SILENT, no_interactive=True``)
    in-process instead of spawning a subprocess. **Blocks until the
    pipeline completes** and returns a compact
    :class:`TypedRunResult` carrying ``status``, ``halt_reason``, and
    the ordered event-spine ``kind`` values.

    Scope:
      * ``mock=true`` only — pilot does not drive real-provider runs.
        For real runs use ``orcho_run_start``; it spawns a background
        subprocess, returns immediately, and supports
        ``orcho_run_watch`` / ``orcho_run_cancel``.
      * Single-project; cross-project is not in scope for this pilot.
      * Best for short flows: mock smokes, fixture generation,
        integration scaffolding.

    Inputs:
      ``task``: inline task description (non-empty).
      ``project_dir``: absolute or MCP-cwd-relative project root.
      ``output_dir``: absolute path where ``meta.json`` / ``events.jsonl``
                      / ``progress.log`` will land.
      ``profile``: pipeline profile name (default ``"task"`` — an
                   internal scoped profile, not a public work-kind
                   choice; used here only because it is the leanest
                   mock-completable shape for this pilot. For real
                   runs select a semantic profile via
                   ``orcho_run_start``).
      ``mock``: must be ``True`` (pilot constraint; see scope above).
      ``max_rounds``: rounds budget; default ``1``.

    Response shape (compact on purpose — call ``orcho_run_status`` /
    ``orcho_run_events_tail`` with the returned ``run_id`` for the
    full surface):

      ``run_id``: pipeline session identifier.
      ``output_dir``: absolute path to the run directory.
      ``status``: ``done`` / ``failed`` / ``awaiting_phase_handoff`` /
                  ``halted`` — read from the persisted session, never
                  parsed from any transcript.
      ``halt_reason``: structured halt reason; ``None`` on the done path.
      ``event_kinds``: ordered ``kind`` values from ``events.jsonl``;
                       the canonical spine is ``run.start`` →
                       (``phase.start`` / ``phase.end``)+ →
                       ``run.end``. Presence under SILENT proves the
                       event sink stayed wired.

    No stdout / stderr is captured or parsed by this tool. Status is
    pulled from the in-memory session dict; events come from the file
    sink. The MCP stdio transport invariant (no stdout pollution from
    handlers) is upheld by the boundary's SILENT contract.
    """
    return run_project_typed_silent(
        task=task,
        project_dir=project_dir,
        output_dir=output_dir,
        profile=profile,
        mock=mock,
        max_rounds=max_rounds,
    )


@mcp.tool()
async def orcho_run_project_typed_async(
    task: str,
    project_dir: str,
    profile: str = "task",
    mock: bool = True,
    max_rounds: int = 1,
) -> TypedRunStartedResult:
    """Spawn a typed silent run in the background; return run_id immediately.

    Non-blocking sibling to ``orcho_run_project_typed``. The pipeline
    body executes inside ``asyncio.to_thread`` so the MCP server's
    event loop stays responsive while the run is in flight. Returns
    a :class:`TypedRunStartedResult` with ``status="running"``; the
    final state is discovered by polling the existing read tools.

    Workspace-aware: the run directory lives at
    ``<workspace>/runspace/runs/<run_id>/`` (workspace resolved via
    ``$ORCHO_WORKSPACE`` or walk-up). The directory name **is** the
    ``run_id``, so the standard read tools resolve the run by id:

      * ``orcho_run_status(run_id)`` — summary meta + metrics; flips to
        ``done`` / ``failed`` / ``awaiting_phase_handoff`` once the
        background task settles.
      * ``orcho_run_events_tail(run_id)`` — incremental events.
      * ``orcho_run_metrics(run_id)`` — phase / cost breakdown.

    Differences vs ``orcho_run_start`` (subprocess-backed):
      * In-process execution via the orcho-core typed silent
        boundary; no detached subprocess, no pid, no signal-based
        cancel surface for this pilot.
      * Mock provider only. Real-provider runs continue to use
        ``orcho_run_start``.
      * No ``progressToken`` notifications yet — observation is
        polling-based against the existing read tools.

    Differences vs ``orcho_run_project_typed`` (blocking sibling):
      * Returns immediately rather than blocking until completion.
      * Derives ``output_dir`` from the workspace instead of taking
        it as an input; the blocking sibling keeps the explicit
        ``output_dir`` shape for callers that own the path layout
        themselves.

    Pilot constraints: one async run in flight at a time per MCP
    server process (the ``ORCHO_RUN_ID`` env var is serialized at
    start). Concurrent async runs are part of the future widening
    story, not this slice.
    """
    return await start_project_typed_silent_async(
        task=task,
        project_dir=project_dir,
        profile=profile,
        mock=mock,
        max_rounds=max_rounds,
    )


@mcp.tool()
async def orcho_run_resume(
    run_id: str,
    profile: str | None = None,
    runtime_override: RuntimeOverrideArg | None = None,
) -> RunResumeResult | ResumeBlockedResult | ResumePendingDecisionResult:
    """Continue an interrupted run by spawning a new pipeline subprocess
    that loads the existing checkpoint via ``--resume``.

    ``runtime_override=None`` (default): a plain checkpoint resume — the run
    continues under its original per-phase runtimes/models. This is the
    overwhelmingly common case.

    ``runtime_override={phase, runtime, model}``: deliver an operator
    *replace* decision after a terminal provider-access failure.
    Before the resume subprocess spawns, the chosen pair is validated against
    the phase's configured replacement candidates and persisted into the run's
    durable ``meta.json``; the resumed pipeline re-reads that record and applies
    the override to exactly the named phase. A non-candidate pair aborts the
    resume (no silent fallback). Pass the value verbatim from the *replace*
    ``next_actions`` Action surfaced by ``orcho_run_status`` /
    ``orcho_run_evidence`` on a provider-access failure.

    ``profile=None`` (default): inherit the original run's profile from
    ``meta.profile``. This is the right default for most callers —
    review and final-acceptance prompt envelopes depend on the active
    profile (``feature`` carries the full plan/validate envelope;
    leaner scoped profiles do not), and resuming under a different
    profile silently changes which prompt parts the reviewer agent
    sees. Runs without a recorded profile fall back to the semantic
    default ``"feature"``.

    ``profile="<name>"`` is a deliberate profile switch — e.g. pass
    ``"small_task"`` for a lean scoped continuation, or ``"planning"``
    to refine the plan only.

    A pre-flight guard classifies the run before spawning, so the typed
    ``resume_outcome`` carries one of these outcomes (the success
    ``outputSchema`` is unchanged — an inspect-only run is signalled via a
    typed error, see below, never as a success member):

    - ``applied`` (:class:`RunResumeResult`) — a resumable run (running
      restart / ``failed`` / ``interrupted`` / non-terminal ``halted``)
      spawned a fresh subprocess; ``pid`` / ``run_dir`` / ``started_at`` /
      ``command`` are populated. ``suggested_next_action`` is a ready
      ``ready_call`` to ``orcho_run_watch`` (pre-filled with ``run_id``) so
      the client can re-enter the watch loop on the resumed subprocess.
    - ``pending_decision`` (:class:`ResumePendingDecisionResult`) — paused
      on ``awaiting_phase_handoff`` with no recorded decision; resolve it
      with ``orcho_phase_handoff_decide`` first, then resume.
    - ``superseded_by_child`` (:class:`ResumeBlockedResult`) — a newer
      unfinished follow-up child is continuing this run; resume that child
      (``recommended_run_id``) instead of this parent. No subprocess spawns.
    - ``rejected_terminal`` (:class:`ResumeBlockedResult`) — the run is
      terminal; resuming is inert, so this is **not** success-shaped (no
      ``pid``). Inspect via ``orcho_run_status`` / ``orcho_run_evidence``
      instead. No subprocess spawns.

    A run that was NOT started by this MCP server (no durable
    ``mcp_supervisor.json`` with a resolvable ``project_dir`` —
    ``control='inspect_only'`` on ``orcho_run_diagnose``) is refused BEFORE the
    supervisor is touched: it raises ``InspectOnlyControlError`` rather than
    returning, so the success ``outputSchema`` above is unchanged. MCP cannot
    resume such a foreign / CLI-started run; manage it through the CLI that
    started it and, from MCP, inspect it via ``orcho_run_status`` /
    ``orcho_run_evidence``. Read the typed ``control`` / ``control_reason``
    classification first with ``orcho_run_diagnose``.

    Errors:
      - InspectOnlyControlError — the run is ``inspect_only`` (not started by
        this MCP server); resume is refused before any spawn. The carried
        ``result`` holds the typed read-only next actions.
      - RunNotFoundError — only when ``run_id`` is genuinely unresolvable /
        corrupt (a foreign run dir is classified ``inspect_only`` above, not
        raised).
    """
    return await resume_run(
        run_id, profile=profile, runtime_override=runtime_override,
    )


@mcp.tool()
async def orcho_phase_handoff_decide(
    run_id: str,
    handoff_id: str,
    action: str,
    feedback: str | None = None,
    note: str | None = None,
    ctx: Context | None = None,
) -> PhaseHandoffDecideResult:
    """Resolve a pipeline paused at ``status=awaiting_phase_handoff``.

    Pure state transition — never spawns a process. The flow:

      1. The pipeline pauses because a phase's declared ``handoff``
         policy fired (e.g. ``human_feedback_on_reject`` on
         ``validate_plan`` after the plan loop exhausted its budget).
         ``meta.status`` becomes ``awaiting_phase_handoff`` and
         ``meta.phase_handoff`` carries the active payload, including
         ``id`` (the ``handoff_id``) and ``available_actions``.
      2. Caller reads the active payload via ``orcho_run_status`` and
         decides: ``continue`` (manual override), ``retry_feedback``
         (one extra human-directed plan round; requires ``feedback``),
         ``continue_with_waiver`` (accept the rejected verdict and
         proceed, recording a durable operator waiver; requires
         ``feedback``), or ``halt`` (terminate the run). ``note`` is
         optional audit. If a client advertises MCP form elicitation
         and calls ``retry_feedback`` or ``continue_with_waiver``
         without ``feedback``, Orcho requests the missing feedback
         natively. Clients without elicitation support ask the user in
         chat and pass ``feedback`` explicitly.
      3. The decision is persisted to
         ``<run_dir>/phase_handoff_decisions/{safe_handoff_id}.json``.
         For ``halt``, ``meta.status`` is flipped to ``halted``
         synchronously and ``meta.phase_handoff`` is cleared.
      4. ``continue`` / ``retry_feedback`` / ``continue_with_waiver`` do
         not advance the run on their own. Follow up with
         ``orcho_run_resume`` to actually resume execution. ``halt`` is
         terminal — no resume.

    Decisions are exact-payload idempotent: the same
    ``(handoff_id, action, feedback, note)`` may be replayed and
    returns the persisted record unchanged. Any field divergence for
    the same ``handoff_id`` is a conflict, not a silent overwrite.

    Args:
        run_id: the paused run.
        handoff_id: id from the active ``meta.phase_handoff.id``
            (e.g. ``"validate_plan:plan_round:2"``).
        action: ``"continue"``, ``"retry_feedback"``,
            ``"continue_with_waiver"``, or ``"halt"``. Must be in the
            active handoff's ``available_actions``.
        feedback: human direction injected into the next round (or the
            waiver text). Required for ``retry_feedback`` and
            ``continue_with_waiver``; rejected for ``continue`` / ``halt``.
        note: free-form audit text.

    A run NOT started by this MCP server (no durable ``mcp_supervisor.json``
    with a resolvable ``project_dir`` — ``control='inspect_only'`` on
    ``orcho_run_diagnose``) is refused before any SDK call or decision-artifact
    write by raising ``InspectOnlyControlError`` — it does NOT widen this tool's
    success ``outputSchema``. MCP can only inspect such a foreign / CLI-started
    run; manage it through the CLI that started it and inspect it from MCP via
    ``orcho_run_status`` / ``orcho_run_evidence``. Read the typed ``control`` /
    ``control_reason`` classification first with ``orcho_run_diagnose``.

    Errors:
      - InspectOnlyControlError — the run is ``inspect_only`` (not started by
        this MCP server); refused before any SDK call or decision-artifact
        write. The carried ``result`` holds the typed read-only next actions.
      - RunNotFoundError — unknown run_id.
      - InvalidPlanError — ``action`` not canonical, ``retry_feedback``
        or ``continue_with_waiver`` without feedback, ``handoff_id``
        mismatch, run not in ``awaiting_phase_handoff``, or a different
        decision already recorded for the same ``handoff_id``.
    """
    return await decide_phase_handoff_with_elicitation(
        run_id, handoff_id, action, feedback=feedback, note=note, ctx=ctx,
    )


@mcp.tool()
def orcho_handoff_advice(
    run_id: str,
    handoff_id: str | None = None,
) -> HandoffAdviceResult:
    """Ask the read-only advisor for a recommendation on a paused phase handoff.

    For a run paused at ``status=awaiting_phase_handoff`` on a rejected /
    incomplete verdict, this runs orcho-core's one-shot read-only advisor and
    returns its typed recommendation: ``recommended_action`` (``continue`` /
    ``retry_feedback`` / ``halt`` / ``continue_with_waiver``), ``confidence``,
    ``rationale``, ``retry_feedback`` text, ``risks``, ``expected_files``,
    ``operator_note``, ``parse_warnings``, a ``safety`` classification, the
    durable ``advice_artifact`` path, the ``provenance_note``, and a
    deterministic ``ready_next_action``.

    Read-only by contract: the advisor writes exactly ONE durable artifact (the
    advice record) and NEVER records a phase-handoff decision, flips
    ``meta.status``, or auto-applies anything. This tool only recommends.

    ``ready_next_action`` is a pre-filled suggestion you MAY forward — it is NOT
    applied here:
      - ``recommended_action == 'retry_feedback'`` → a ``ready_call`` to the
        EXISTING ``orcho_phase_handoff_decide`` with ``action='retry_feedback'``,
        ``feedback`` set to the advisor's retry text, and a MANDATORY
        ``note=<provenance_note>`` linking the decision back to the advice
        artifact. No new verb is introduced; forwarding it (and only then) records
        the retry.
      - ``continue`` / ``halt`` → a ``ready_call`` mirroring that verb;
        ``continue_with_waiver`` → ``operator_input_required`` (you must supply
        the waiver verdict as ``feedback``).

    ``handoff_id`` is optional — when omitted, the run's active handoff is used;
    when supplied it must match the active handoff id.

    Errors:
      - RunNotFoundError — unknown run_id.
      - WorkspaceNotResolvedError — no workspace could be resolved.
      - InvalidPlanError — no active handoff, a mismatched ``handoff_id``, a run
        not paused on a decidable handoff, or a handoff ineligible for advice
        (wrong trigger/verdict, no ``retry_feedback`` offered, no finding /
        last output).
    """
    return request_advice(run_id, handoff_id)


@mcp.tool()
def orcho_delivery_decide(
    run_id: str,
    action: Literal["approve", "apply", "fix", "skip", "halt"],
    note: str | None = None,
) -> DeliveryDecideResult:
    """Resolve a parked post-release delivery / correction gate.

    Pure state transition through the orcho-core SDK — never spawns a
    process and never applies a patch in MCP. Use ``orcho_delivery_gate`` first
    to inspect the gate and read its ``available_actions`` / ``blocked_actions``.

    Actions:
      - ``approve``: commit the retained worktree diff into the target checkout.
      - ``apply``: apply the retained diff without committing it.
      - ``fix``: mark the run correction-ready after a rejected release verdict.
      - ``skip``: close the gate without changing the target checkout.
      - ``halt``: leave the retained worktree in place for manual inspection.

    The return value mirrors core's typed decision result. Refused business
    decisions are returned as ``accepted=False`` with a ``blocker`` such as
    ``no_pending_delivery_gate``, ``release_blocked``, or
    ``verification_blocked``; missing runs / bad arguments use MCP errors.
    """
    return decide_delivery(run_id, action, note=note)


@mcp.tool()
def orcho_run_evidence(
    run_id: str,
    slice: str = "all",
    severity_min: str | None = None,
    phases: list[str] | None = None,
) -> EvidenceResult:
    """Inspect a run via typed slices — no raw log scraping required.

    The full evidence bundle (``collect_evidence``) is exhaustive; this
    tool surfaces narrow projections control-loop clients actually
    need: what the plan said, which findings blocked the run, which
    commands the pipeline shelled out to, what artifacts landed, why
    the run halted, and the cross-run alias linkage for cross-project
    parents.

    ``slice``:
      - ``"all"`` (default) — every slice populated in one response.
      - ``"plan"`` — plan summary only.
      - ``"findings"`` — flattened findings list (filterable).
      - ``"commands"`` — pipeline shell-outs.
      - ``"artifacts"`` — files the run wrote.
      - ``"errors"`` — errors + halt reason.
      - ``"sub_runs"`` — cross-run child alias links (empty for
        single-project runs).
      - ``"receipts"`` — per-subtask delivery receipts for a
        ``subtask_dag`` run: state (``done|incomplete|failed|skipped``)
        plus the done-criteria self-attestation (``criteria_report``,
        ``attestation_summary``, ``attestation_error``). A subtask that
        executed but did not close its done-criteria is ``incomplete``
        with the reason in ``attestation_error``. Empty for
        ``whole_plan`` runs.
      - ``"verification_receipts"`` — durable verification-environment
        receipts for the developer-side phases: which
        interpreter (``python``) and working directory (``cwd``) ran the
        checks, the import checks (name / expected / actual / passed),
        the exact commands + exit codes, the clean-tree note
        (``temp_env_outside_checkout``), and the on-disk
        ``artifact_path``. Empty when the run recorded no receipts.
      - ``"verification_timeline"`` — the official verification-gate
        timeline as typed data: per-gate ``status`` (exactly one of
        ``PASS`` / ``FAIL`` / ``MISSING`` / ``STALE`` / ``SKIPPED`` /
        ``FRESH`` — a manual/operator-only gate is ``SKIPPED`` with
        ``policy='manual_only'``), each missing/stale/failed required gate
        carrying its own ``rerun_hint`` + ``searched_run_dirs``, plus the
        residual / manual-only / inherited aggregates and the auto-run
        events. ``has_contract=False`` when the project declares no
        verification contract.
      - ``"verification_cockpit"`` — the SAME verification gates as a typed
        cockpit (built from one shared SDK read, never hiding the timeline):
        a header (``has_contract`` / ``mode`` / ``envs`` / ``policy_summary``
        / ``effect``) plus one row per gate. Each row keeps the command name
        AND its planning properties visible together — ``hook``/phase,
        ``trigger``, ``policy``, ``required``, ``gate_class`` + ``class_source``,
        the same six-value ``status``, ``env``, and the deciding-receipt
        evidence (``receipt_path`` / ``inherited`` / ``source_run_id``) with
        ``stale_reason`` / ``rerun_hint`` where applicable. ``trigger`` is
        derived deterministically: ``operator_only`` for a manual-only gate
        (membership or ``policy='manual_only'``) — present on purpose, NOT an
        automation failure; ``auto`` when the run's automation acted on the
        command; else ``manual``. ``has_contract=False`` when the project
        declares no verification contract.
      - ``"handoff_advice"`` — Stage 0/1 phase-handoff advisor evidence:
        one record per advisor call (``handoff_id`` / ``phase`` /
        ``recommended_action`` / ``applied_action`` / ``confidence`` /
        ``resolved`` / ``repeated`` / ``outcome`` / ``finding_fingerprint`` /
        token-usage + cost / ``advice_artifact``) plus an aggregate ``summary``
        (``calls`` / ``applied_retries`` / ``resolved_retries`` / ``repeated`` /
        ``stopped`` / ``unknown`` / ``usage``). ``resolved`` is tri-state
        (True / False / None). A run with no advisor surface returns an empty
        slice (``calls=[]``, zeroed summary), never an error.

    ``severity_min`` (only honoured when findings are returned):
    minimum-criticality cutoff. ``"P0"`` returns only P0; ``"P1"``
    returns P0 + P1; etc. ``None`` returns all severities.

    ``phases`` (only honoured for findings): restrict to specific
    finding-bearing phases (``plan_qa``, ``review``, ``final_qa``,
    ``compliance_check``). ``None`` returns findings from all four.

    Errors:
      - RunNotFoundError — unknown run_id.
      - InvalidPlanError — invalid ``slice`` value or invalid
        ``severity_min`` value.
    """
    return inspect_run_evidence(
        run_id, slice=slice, severity_min=severity_min, phases=phases,
    )


@mcp.tool()
def orcho_run_diff(
    run_id: str,
    mode: Literal["preview", "stat", "full"] = "preview",
    path: str | None = None,
    phase: str | None = None,
    max_bytes: int = 200_000,
) -> RunDiffResult:
    """Read a captured ``diff.patch`` artifact for a run.

    The pipeline writes diff artifacts at run lifecycle time; this
    tool is the read side. Missing artifact is a typed
    ``found=False`` result — not a JSON-RPC error — because clean
    runs / quiet phases and runs predating the artifact are all
    valid.

    ``mode``:
      - ``"preview"`` (default) — Claude-style grouped view with
        per-file ``+A -R`` headers; good for context windows.
      - ``"stat"`` — per-file ``+A -R`` table only.
      - ``"full"`` — raw unified patch; suitable for piping to
        ``git apply`` after byte-cap stripping. Never colored.

    ``path`` (optional): restrict to files at this path. Exact match
    first; falls back to prefix match. Matches renames and deletes by
    either old or new name. Empty / whitespace-only string raises
    ``InvalidPlanError``.

    ``phase`` (optional): artifact key.
      - ``None`` (default) reads the run-level cumulative
        ``diff.patch`` — today's behaviour, unchanged.
      - ``"<phase>"`` reads the per-phase artifact
        ``phases/<phase>/diff.patch`` written by the pipeline during
        that phase (e.g. ``"implement"``, ``"repair_changes"``).
      Quiet phase (no artifact captured) → ``found=False``, never a
      silent fallback to the cumulative diff.
      Empty / whitespace-only, or values containing ``/``, ``\\``,
      or ``..``, raise ``InvalidPlanError``.

    ``max_bytes`` caps ``content`` in bytes. Default 200_000; hard
    ceiling 2_000_000. ``0`` or above-ceiling values are rejected
    (not silently clamped) so callers know they hit the limit. The
    truncation is UTF-8 safe.

    The result echoes ``scope`` (``"run"`` or ``"phase"``) and
    ``phase`` so clients don't need to track what they asked for.

    Errors:
      - RunNotFoundError — unknown run_id.
      - WorkspaceNotResolvedError — no $ORCHO_WORKSPACE / $ORCHO_WORKTREE.
      - InvalidPlanError — bad ``max_bytes`` (≤0, >2_000_000), empty
        ``path``, or empty / traversal-bearing ``phase``.
    """
    return inspect_run_diff(
        run_id, mode=mode, path=path, phase=phase, max_bytes=max_bytes,
    )


@mcp.tool()
def orcho_run_diagnose(run_id: str) -> RunDiagnosis:
    """Diagnose a run's resume situation before acting on it — read-only.

    Returns a typed verdict so a control-loop client never has to guess
    whether resuming is safe, wasted, or wrong. Call this BEFORE any risky
    ``orcho_run_resume``: it classifies the run deterministically and hands
    back unambiguously typed next steps.

    ``condition`` (first-match priority):
      - ``active`` — running; watch / poll it (no resume needed).
      - ``needs_decision`` — paused on ``awaiting_phase_handoff``; an operator
        must record a phase-handoff decision before it can resume.
      - ``resume_inert_terminal`` — terminal (terminal success or a terminal
        halt reason); resuming is inert, so only inspection is suggested.
      - ``superseded_by_child`` — a newer unfinished follow-up child is
        continuing this run; resume the child (``recommended_run_id``), not
        this parent.
      - ``blocked_worktree`` — a follow-up blocked because the parent's
        undelivered diff is not replayable here; resume the parent when known,
        otherwise inspect (never a blind resume).
      - ``halted`` / ``failed`` / ``interrupted`` — a resumable non-terminal
        stop; resume this run or inspect its errors.

    ``control`` is a SEPARATE, orthogonal axis from ``condition``: whether this
    MCP server can actually mutate the run. ``mcp_controllable`` means the run
    carries durable ``mcp_supervisor.json`` state with a resolvable project_dir
    (it was started by this server, so resume / decide can act on it);
    ``inspect_only`` means the run was NOT started by this MCP server and has no
    durable supervisor metadata (a foreign / CLI-started run dir), so MCP can
    only inspect it — a suggested ``orcho_run_resume`` cannot be applied even
    when the ``condition`` is resumable. ``control_reason`` is the one-line fact
    behind the verdict. ``control`` is ``None`` only when the classification
    could not be read (never defaulted to controllable).

    ``next_actions`` are typed by call-readiness via the ``kind`` field, which
    clients MUST branch on instead of reading ``intent`` prose:
      - ``kind="ready_call"`` — ``args`` already carry every required
        parameter of the target tool, so the call is safe to forward verbatim
        (e.g. ``orcho_run_resume`` with ``run_id``).
      - ``kind="operator_input_required"`` — a final decision argument is
        intentionally omitted; ``choices`` and/or ``input_schema`` describe the
        operator input still needed (e.g. the required ``feedback`` for a
        ``retry_feedback`` decision). Such records are never ``ready_call``.

    Read-only: this tool spawns no process and mutates no state.

    Raises RunNotFoundError if ``run_id`` doesn't exist on disk.
    """
    return inspect_run_diagnosis(run_id)


@mcp.tool()
def orcho_delivery_gate(run_id: str) -> DeliveryGateProjection:
    """Inspect an Orcho-managed run's post-release delivery / correction gate.

    Read-only typed projection that tells an agent, WITHOUT parsing terminal
    prose, whether a run is sitting at an Orcho-managed delivery decision or
    is just a direct checkout edit. The classification (``kind``), available
    actions, blocked actions, and default action are derived from
    orcho-core's SDK ``delivery_decision_state``:

      - ``delivery_decision_required`` — a pending delivery on an APPROVED
        release; core may allow approve / apply / skip / halt
        (``approve`` is the only action that creates a commit).
      - ``correction_decision_required`` — a rejected release or a
        ``fix_requested`` state; core normally allows fix / halt for a current
        rejected release and lists any refused actions in
        ``blocked_actions``. This is an available correction-flow state, NOT
        an executed delivery.
      - ``direct_checkout_or_running`` — no pending commit-delivery gate
        (terminal delivery, a direct checkout edit, or a still-running run);
        no approve / apply / fix is offered.

    ``diff`` summarises the retained change (``files_changed`` +
    ``changed_paths`` + ``untracked_paths``). If a secondary artifact
    (``commit_decisions`` / ``diff.patch``) is missing or unreadable on a
    pending gate, ``diff.degraded`` is ``True`` and ``message`` names the
    missing artifact — the gate ``kind`` is never hidden by that.

    ``next_actions`` carries one ``ready_call`` to ``orcho_delivery_decide``
    per available action. Each record contains the full required args for the
    selected decision call. MCP itself never applies the retained diff; the
    decision tool delegates to orcho-core's SDK.

    Raises RunNotFoundError when ``run_id`` doesn't exist on disk; a missing
    or corrupt ``meta.json`` yields a ``direct_checkout_or_running``
    projection rather than an error.
    """
    return project_delivery_gate(run_id)


@mcp.tool()
async def orcho_run_cancel(run_id: str, mode: str = "graceful") -> CancelResult:
    """Stop a running pipeline.

    ``mode="graceful"`` sends SIGTERM to the run's process group; pipeline
    catches it, flushes the checkpoint, and emits a ``run.interrupted``
    event. ``mode="hard"`` sends SIGKILL; in-flight LLM HTTP requests drop
    immediately and the checkpoint reflects only fully-completed phases.

    Works for runs spawned this server lifetime as well as orphans picked
    up by restart-recovery (cancel via raw ``os.kill`` on the persisted pid).
    """
    return await cancel_run(run_id, mode=mode)


__all__ = [
    "orcho_workspace_info",
    "orcho_workspace_state",
    "orcho_workspace_pending_decisions",
    "orcho_run_history",
    "orcho_run_status",
    "orcho_run_metrics",
    "orcho_run_events_tail",
    "orcho_run_events_summary",
    "orcho_run_watch",
    "orcho_plan_validate",
    "orcho_skills_list",
    "orcho_prompts_resolve",
    "orcho_profiles_list",
    "orcho_workflows_list",
    "orcho_run_start",
    "orcho_run_resume",
    "orcho_run_cancel",
    "orcho_phase_handoff_decide",
    "orcho_delivery_decide",
    "orcho_run_evidence",
    "orcho_run_diff",
    "orcho_run_diagnose",
    "orcho_delivery_gate",
]
