"""orcho_mcp.services.run_reads — direct run-read primitives.

Sync public functions backing four MCP tool handlers
(``orcho_run_status``, ``orcho_run_metrics``, ``orcho_run_events_tail``,
``orcho_workspace_state``) that live in ``orcho_mcp.tools`` as thin
shims. Each entry wraps an SDK / event-log / advisory-state read with
the canonical MCP error mapping and packs into the
``orcho_mcp.schemas`` response model.

SDK aliases ``load_status`` and ``get_run_metrics`` live here so the
MCP adapter layer does not call the SDK directly. The
``services.run_events.read_run_events`` provides the raw events-tail
path through the core SDK event surface.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sdk import (
    get_run_metrics as _sdk_get_run_metrics,
    load_cross_execution_graph as _sdk_load_cross_execution_graph,
    load_cross_execution_graph_state as _sdk_load_cross_execution_graph_state,
    load_status as _sdk_load_status,
)

from orcho_mcp.errors import RunNotFoundError, WorkspaceNotResolvedError
from orcho_mcp.observe.observation import workspace_dir_or_none
from orcho_mcp.observe.summary import build_latest_run_events_summary
from orcho_mcp.schemas import (
    ArtefactRefRecord,
    AutoDetectProjection,
    CrossExecutionGraphCompileIdentityRecord,
    CrossExecutionGraphExecutorPolicyRecord,
    CrossExecutionGraphNodeRecord,
    CrossExecutionGraphOperationRecord,
    CrossExecutionGraphRecord,
    EventRecord,
    EventsTailResult,
    FollowupLineage,
    RecoveryLineage,
    RecoveryRecommendation,
    RunMetrics,
    RunStatus,
    WorkspaceMcpStateResult,
    WorkspaceRunStateRecord,
    WorktreeContinuity,
)
from orcho_mcp.schemas.read import PhaseCost, RunEconomics
from orcho_mcp.services.errors import map_sdk_errors
from orcho_mcp.services.meta_summary import summarize_run_meta
from orcho_mcp.services.run_events import read_run_events
from orcho_mcp.services.run_lineage import (
    RecoveryLineageProjection,
    project_recovery_lineage,
)
from orcho_mcp.services.run_lookup import find_run_dir
from orcho_mcp.services.run_projection import (
    build_provider_pressure,
    merged_halt_reason_from_meta,
    merged_status_from_meta,
    project_auto_detect,
    project_followup_lineage,
    project_pending_handoff,
    project_provider_pressure,
    project_worktree_continuity,
)
from orcho_mcp.workspace_state import read_workspace_state


def _enum_value(value: object) -> str:
    """Return a public SDK enum's stable wire value without importing core types."""
    return str(getattr(value, "value", value))


def _project_cross_execution_graph(
    run_id: str,
    run_dir: Path,
) -> CrossExecutionGraphRecord | None:
    """Join immutable graph structure to SDK-derived state without reconstruction.

    The artifact-presence check deliberately does not open or decode the file:
    no graph means a normal mono/pre-graph status, while an existing malformed
    artifact is delegated to the SDK and mapped as ``InvalidPlanError``.
    """
    if not (run_dir / "cross_execution_graph.json").exists():
        return None

    with map_sdk_errors(run_id):
        graph = _sdk_load_cross_execution_graph(
            run_id, runs_dir=run_dir.parent, cwd=None,
        )
        state = _sdk_load_cross_execution_graph_state(
            run_id, runs_dir=run_dir.parent, cwd=None,
        )
        graph_identities = tuple(node.identity for node in graph.nodes)
        state_identities = tuple(node.identity for node in state.nodes)
        if graph_identities != state_identities:
            raise ValueError(
                "cross execution graph structure/state identity mismatch"
            )

        return CrossExecutionGraphRecord(
            compile_identity=CrossExecutionGraphCompileIdentityRecord(
                schema_version=graph.compile_identity.schema_version,
                fingerprint=graph.compile_identity.fingerprint,
            ),
            nodes=[
                CrossExecutionGraphNodeRecord(
                    identity=node.identity,
                    kind=_enum_value(node.kind),
                    dependencies=list(node.dependencies),
                    owner=_enum_value(node.owner),
                    executor=CrossExecutionGraphExecutorPolicyRecord(
                        executor=_enum_value(node.executor.executor),
                        handler=node.executor.handler,
                        enabled=node.executor.enabled,
                        run=node.executor.run,
                        on_skip=node.executor.on_skip,
                        mode=node.executor.mode,
                    ),
                    required=node.required,
                    status=_enum_value(node_state.status),
                    reason=_enum_value(node_state.reason),
                    alias=node_state.alias,
                    operations=[
                        CrossExecutionGraphOperationRecord(
                            alias=operation.alias,
                            executor=_enum_value(operation.executor),
                            phase=operation.phase,
                            hook=operation.hook,
                            command=list(operation.command),
                        )
                        for operation in node_state.operations
                    ],
                )
                for node, node_state in zip(graph.nodes, state.nodes, strict=True)
            ],
        )


def _recovery_recommendation(
    rec: RecoveryLineageProjection,
) -> RecoveryRecommendation | None:
    """Build the wire ``RecoveryRecommendation`` from the lineage projection.

    Projected from the SAME ``services.run_lineage`` resolver that backs
    ``orcho_run_diagnose`` (no re-derivation), so the recommendation a captain
    sees on ``orcho_run_status`` matches diagnose for the same run. Returns
    ``None`` for a trivial state — an ordinary run that continues itself with
    no terminality and no active child (``recommended_next_action is None``) —
    so the high-frequency poll payload stays clean.
    """
    if rec.recommended_next_action is None:
        return None
    return RecoveryRecommendation(
        continuation_subject=rec.continuation_subject,
        recommended_next_action=rec.recommended_next_action,
        recommended_run_id=rec.recommended_run_id,
        reason=rec.reason,
        lineage=RecoveryLineage(
            source_run_id=rec.source_run_id,
            source_status=rec.source_status,
            source_resumable=rec.source_resumable,
            active_child_run_id=rec.active_child_run_id,
            plan_subject_available=rec.plan_subject_available,
            missing_facts=list(rec.missing_facts),
        ),
    )


def get_run_status(
    run_id: str,
    include: list[str] | None = None,
) -> RunStatus:
    """Summary meta + metrics snapshot for a single run.

    See ``orcho_run_status`` docstring in ``orcho_mcp.tools`` for the
    wire contract. This module owns the implementation.

    ``meta`` is projected to a summary-only shape by default (phase
    bodies elided to size/count markers) so the supervisor's polling
    loop stays cheap. ``include`` re-admits specific body families; see
    :func:`orcho_mcp.services.meta_summary.summarize_run_meta`.
    """
    with map_sdk_errors(run_id):
        run_dir = find_run_dir(run_id)
        s = _sdk_load_status(run_id, runs_dir=run_dir.parent, cwd=None)

    # Wire fidelity: MCP returns ``None`` (not ``{}``) when metrics.json
    # is missing. SDK normalises missing files to ``{}``.
    metrics = s.raw_metrics if s.raw_metrics else None

    cross_execution_graph = _project_cross_execution_graph(
        s.run_ref.run_id, s.run_ref.run_dir,
    )

    meta = dict(s.raw_meta or {})
    merged = merged_status_from_meta(meta, s.run_ref.run_dir)
    if merged is not None:
        meta["status"] = merged
    merged_reason = merged_halt_reason_from_meta(meta, s.run_ref.run_dir)
    if merged_reason is not None and not meta.get("halt_reason"):
        meta["halt_reason"] = merged_reason

    # Follow-up lineage: parent linkage + (when present) a recommendation
    # to resume an active follow-up child instead of this run. The
    # projection owns the runs-dir scan + the terminal-status filter; this
    # is a pure pass-through into the wire model.
    lin = project_followup_lineage(s.run_ref.run_id)
    lineage = FollowupLineage(
        run_id=lin.run_id,
        parent_run_id=lin.parent_run_id,
        parent_status=lin.parent_status,
        resume_mode=lin.resume_mode,
        has_active_child_followup=lin.has_active_child_followup,
        active_child_run_id=lin.active_child_run_id,
        active_child_status=lin.active_child_status,
        active_child_handoff_id=lin.active_child_handoff_id,
        recommended_action=lin.recommended_action,
        recommended_run_id=lin.recommended_run_id,
        recommendation=lin.recommendation,
    )

    # Worktree-continuity: project the persisted ``meta['worktree']`` block
    # (mode / diff_source / clean-HEAD warning). Pure transform of the
    # already-loaded meta — no extra read.
    wc = project_worktree_continuity(meta.get("worktree"))
    worktree_continuity = WorktreeContinuity(
        has_worktree=wc.has_worktree,
        subject_mode=wc.subject_mode,
        isolation=wc.isolation,
        path=wc.path,
        diff_source=wc.diff_source,
        blocked=wc.blocked,
        block_message=wc.block_message,
        mode_label=wc.mode_label,
        worktree_preserved=wc.worktree_preserved,
        degraded_reason=wc.degraded_reason,
        is_followup_continuity=wc.is_followup_continuity,
    )

    # Auto-detect projection: typed view of ``meta.auto_detect`` (requested
    # selector + detector outcome + deterministic next_action). ``None`` for a
    # run that did not start through the ``auto-detect`` selector. Pure
    # transform of the already-loaded meta — no extra read; ``next_action`` is
    # already a wire ``NextActionRecord`` so it passes straight through.
    ad = project_auto_detect(meta)
    auto_detect = (
        AutoDetectProjection(
            requested_selector=ad.requested_selector,
            detection_state=ad.detection_state,
            selected_profile=ad.selected_profile,
            selected_mode=ad.selected_mode,
            recommended_profile=ad.recommended_profile,
            recommended_mode=ad.recommended_mode,
            policy=ad.policy,
            confidence=ad.confidence,
            fallback_used=ad.fallback_used,
            confirmation_state=ad.confirmation_state,
            risk_flags=list(ad.risk_flags),
            rationale=ad.rationale,
            error_reason=ad.error_reason,
            fallback_reason=ad.fallback_reason,
            disposition=ad.disposition,
            trusted=ad.trusted,
            next_action=ad.next_action,
            recommended_topology=ad.recommended_topology,
            delivery_scope=ad.delivery_scope,
            projects=list(ad.projects),
            topology_reason=ad.topology_reason,
            topology_next_actions=list(ad.topology_next_actions),
        )
        if ad is not None else None
    )

    # Recovery recommendation: the lineage-aware continuation subject + next
    # action, projected from the SAME ``project_recovery_lineage`` resolver
    # that backs ``orcho_run_diagnose`` so status and diagnose never diverge.
    # ``None`` for a trivial run (no terminality / no active child). Built from
    # the already-resolved meta + cheap SDK readers — no heavy reads added to
    # the high-frequency poll.
    recovery_recommendation = _recovery_recommendation(
        project_recovery_lineage(s.run_ref.run_id),
    )

    # Provider-pressure: the core-typed provider runtime/access failure
    # projected from the SAME ``project_provider_pressure`` source + shared
    # ``build_provider_pressure_next_actions`` helper as ``orcho_run_diagnose``
    # / ``orcho_run_evidence`` / ``orcho_run_events_summary`` so all surfaces
    # agree. ``None`` for a generic failure with no core-typed provider source.
    # The legacy SDK pass-through ``next_actions`` below is untouched — the
    # provider-pressure follow-ups ride in ``provider_pressure.next_actions``.
    provider_pressure = build_provider_pressure(
        project_provider_pressure(s.run_ref.run_id),
    )

    # Live subtask coordinate: reuse the SAME bounded observe walk that backs
    # ``orcho_run_live_status`` (``build_latest_run_events_summary`` → the
    # latest ``subtask.start`` / ``subtask.end`` boundary) so status and
    # live_status report an identical ``current_subtask`` for a run. No new
    # subtask derivation and no core-SDK field; the summary read is the same
    # bounded event walk live_status already performs on the hot poll.
    # ``None`` for a terminal run or a phase with no in-flight subtask.
    current_subtask = build_latest_run_events_summary(
        s.run_ref.run_id,
    ).current_subtask

    # Summary-only projection of meta for the wire. The lineage /
    # worktree projections above already consumed the small top-level
    # blocks they need from the full ``meta``; this trims the heavy phase
    # bodies (plan markdown, agent output, critiques, receipts) that
    # otherwise dominate the polling payload. ``include`` re-admits
    # specific body families; ``include=["all"]`` is full passthrough.
    meta_wire = summarize_run_meta(
        meta, include=frozenset(include or ()),
    )

    pending = project_pending_handoff(s.run_ref.run_id)
    status_actions = [a.to_dict() for a in s.next_actions]
    if pending.is_pending_handoff:
        if pending.decision_state == "recorded":
            status_actions = [{"intent": "Apply the recorded phase-handoff decision.", "tool": "orcho_run_resume", "args": {"run_id": s.run_ref.run_id}, "optional": False, "kind": "ready_call"}]
        elif pending.decision_state == "degraded":
            status_actions = [{"intent": "Inspect the decision-read failure before attempting a mutation.", "tool": "orcho_run_diagnose", "args": {"run_id": s.run_ref.run_id}, "optional": False, "kind": "ready_call"}]
    return RunStatus(
        run_id=s.run_ref.run_id,
        run_dir=str(s.run_ref.run_dir),
        meta=meta_wire,
        metrics=metrics,
        sub_runs=sorted(sp.name for sp in s.sub_projects),
        cross_execution_graph=cross_execution_graph,
        lineage=lineage,
        worktree_continuity=worktree_continuity,
        auto_detect=auto_detect,
        recovery_recommendation=recovery_recommendation,
        # MCP UX A1: pass through orcho-core's state-derived
        # next_actions. The SDK already computed them when building
        # RunStatus (see sdk.status.load_status). Pure pass-through —
        # no transformation, no enrichment — so the suggestions stay
        # consistent across consumers (CLI, MCP, future Web UI).
        next_actions=status_actions,
        decision_state=pending.decision_state if pending.is_pending_handoff else None,
        decision_degraded_reason=pending.decision_degraded_reason if pending.is_pending_handoff else None,
        # SDK enumerates readable artefacts for a resolved run. Pure
        # pass-through to the wire model — ``kind`` narrows from SDK
        # ``str`` to wire ``Literal[...]`` via Pydantic validation,
        # so an unknown kind from a newer SDK would surface as a
        # ValidationError here rather than as a silent wire surprise.
        artefacts=[
            ArtefactRefRecord(
                kind=a.kind,
                uri=a.uri,
                mime=a.mime,
                size_bytes=a.size_bytes,
            )
            for a in s.artefacts
        ],
        provider_pressure=provider_pressure,
        current_subtask=current_subtask,
    )


def get_run_metrics(run_id: str) -> RunMetrics:
    """Raw metrics.json for a single run (token counts, durations, per-phase).

    See ``orcho_run_metrics`` docstring in ``orcho_mcp.tools`` for the
    wire contract.

    Module-level alias: the SDK helper is imported as
    ``_sdk_get_run_metrics`` so the public service entry can keep its
    natural ``get_run_metrics`` name without shadowing.
    """
    with map_sdk_errors(run_id):
        run_dir = find_run_dir(run_id)
        m = _sdk_get_run_metrics(run_id, runs_dir=run_dir.parent, cwd=None)
    if not m.raw:
        raise RunNotFoundError(f"no metrics.json for run {run_id}")
    return RunMetrics(run_id=run_id, metrics=m.raw)


def _coerce_int(raw: object, default: int = 0) -> int:
    """Coerce a metrics value to ``int``, rejecting bool and junk.

    ``bool`` is an ``int`` subclass; a stray ``True`` token/attempt counter
    would silently read as ``1``, so it is rejected to the default.
    """
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    return default


def _coerce_float(raw: object, default: float = 0.0) -> float:
    """Coerce a metrics value to ``float``, rejecting bool and junk."""
    if isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        return float(raw)
    return default


def _phase_total_tokens(entry: dict[str, Any]) -> int:
    """Per-phase total tokens — prefer the rollup field, else tokens_in+out.

    Older / partial metrics rows may omit ``total_tokens`` while still
    carrying ``tokens_in`` / ``tokens_out``; fall back to their sum so the
    economics view never under-counts a phase that recorded split counters.
    """
    if "total_tokens" in entry:
        return _coerce_int(entry.get("total_tokens"))
    return _coerce_int(entry.get("tokens_in")) + _coerce_int(entry.get("tokens_out"))


def project_run_economics(metrics: dict[str, Any]) -> RunEconomics:
    """Project a raw ``metrics.json`` dict into the typed run-economics view.

    A pure, thin typing pass over ``RunMetrics.metrics`` — no IO, no SDK
    call, no surface change. The per-phase rows come from the cumulative
    ``metrics['phases']`` rollup (each entry's ``attempts`` defaults to 1),
    and ``retry_rate`` is the per-phase retry surplus normalised by phase
    count::

        retry_rate = (sum(phase.attempts) - n_phases) / n_phases

    so a clean run (every phase ran once) yields ``0.0``; the rate is
    ``0.0`` when there are no phases. Defensive: a non-dict ``phases`` block
    or non-dict entry contributes nothing rather than raising, mirroring the
    projection contract elsewhere in this module.
    """
    raw_phases = metrics.get("phases")
    phases: list[PhaseCost] = []
    if isinstance(raw_phases, dict):
        for name, entry in raw_phases.items():
            if not isinstance(entry, dict):
                continue
            phases.append(
                PhaseCost(
                    phase=str(name),
                    total_tokens=_phase_total_tokens(entry),
                    duration_s=_coerce_float(entry.get("duration_s")),
                    attempts=max(1, _coerce_int(entry.get("attempts"), default=1)),
                ),
            )

    n_phases = len(phases)
    if n_phases:
        total_attempts = sum(p.attempts for p in phases)
        retry_rate = (total_attempts - n_phases) / n_phases
    else:
        retry_rate = 0.0

    return RunEconomics(
        total_tokens=_coerce_int(metrics.get("total_tokens")),
        total_duration_s=_coerce_float(metrics.get("total_duration_s")),
        total_rounds=_coerce_int(metrics.get("total_rounds")),
        retry_rate=retry_rate,
        phases=phases,
    )


def get_run_events_tail(
    run_id: str,
    since_seq: int = 0,
    limit: int = 25,
) -> EventsTailResult:
    """Return events with seq > since_seq, newest events at the end.

    See ``orcho_run_events_tail`` docstring in ``orcho_mcp.tools`` for
    the wire contract.
    """
    all_events = read_run_events(run_id)

    new_events = [e for e in all_events if e.seq > since_seq]
    truncated = new_events[:limit]

    next_seq = truncated[-1].seq if truncated else since_seq

    eof = len(new_events) <= limit  # everything past since_seq fit in this batch

    records = [
        EventRecord(
            seq=e.seq, ts=e.ts, kind=e.kind, phase=e.phase, payload=e.payload,
        )
        for e in truncated
    ]
    return EventsTailResult(run_id=run_id, events=records, next_seq=next_seq, eof=eof)


def get_workspace_mcp_state() -> WorkspaceMcpStateResult:
    """Advisory MCP workspace state — last observed cursor per run.

    See ``orcho_workspace_state`` docstring in ``orcho_mcp.tools`` for
    the wire contract.
    """
    workspace_dir = workspace_dir_or_none()
    if workspace_dir is None:
        raise WorkspaceNotResolvedError(
            "could not resolve workspace directory. "
            "Set $ORCHO_WORKSPACE or run the MCP server from inside an "
            "orcho workspace.",
        )
    state = read_workspace_state(workspace_dir)
    runs = {
        run_id: WorkspaceRunStateRecord(**record)
        for run_id, record in state["runs"].items()
    }
    return WorkspaceMcpStateResult(
        version=state["version"],
        workspace_dir=state["workspace_dir"],
        server_started_at=state["server_started_at"],
        updated_at=state["updated_at"],
        runs=runs,
    )


__all__ = [
    "get_run_events_tail",
    "get_run_metrics",
    "get_run_status",
    "get_workspace_mcp_state",
    "project_run_economics",
]
