"""orcho_mcp.resources.runs — ``orcho://runs[/...]`` resources.

MCP resources covering the runs catalogue and per-run artefacts:
``orcho://runs`` (recent list), ``orcho://runs/{id}/meta``,
``orcho://runs/{id}/metrics``, ``orcho://runs/{id}/events``,
``orcho://runs/{id}/summary``,
``orcho://runs/{id}/parsed_plan.json``,
``orcho://runs/{id}/evidence``, and
``orcho://runs/{id}/diff.patch``. All handlers
delegate to ``services.read_queries`` / ``services.run_artifacts`` —
this module stays a thin URI-to-service adapter with no SDK or file
I/O of its own.
"""
from __future__ import annotations

from orcho_mcp.instance import mcp
from orcho_mcp.observe.summary import build_latest_run_events_summary
from orcho_mcp.resources.helpers import _dump
from orcho_mcp.services.read_queries import get_run_history
from orcho_mcp.services.run_artifacts import (
    get_run_diff_patch,
    get_run_events_ndjson,
    get_run_evidence_raw,
    get_run_meta_raw,
    get_run_metrics_raw,
    get_run_parsed_plan_raw,
    get_run_phase_diff_patch,
)


@mcp.resource(
    "orcho://runs",
    name="orcho_runs",
    description="Recent pipeline runs, newest first.",
    mime_type="application/json",
)
def runs_resource() -> str:
    # ``limit=50`` is the resource-catalogue default; ``get_run_history``
    # itself defaults to 10 (matches the wire-default of orcho_run_history
    # — do not change without regenerating docs/mcp_schema.json).
    return _dump(get_run_history(limit=50))


@mcp.resource(
    "orcho://runs/{run_id}/meta",
    name="orcho_run_meta",
    description="meta.json for a single run.",
    mime_type="application/json",
)
def run_meta_resource(run_id: str) -> str:
    return _dump(get_run_meta_raw(run_id))


@mcp.resource(
    "orcho://runs/{run_id}/metrics",
    name="orcho_run_metrics",
    description="metrics.json for a single run (token counts, durations, per-phase).",
    mime_type="application/json",
)
def run_metrics_resource(run_id: str) -> str:
    return _dump(get_run_metrics_raw(run_id))


@mcp.resource(
    "orcho://runs/{run_id}/events",
    name="orcho_run_events",
    description="Full events.jsonl stream for a run (newline-delimited JSON).",
    mime_type="application/x-ndjson",
)
def run_events_resource(run_id: str) -> str:
    return get_run_events_ndjson(run_id)


@mcp.resource(
    "orcho://runs/{run_id}/summary",
    name="orcho_run_events_summary",
    description="Latest bounded event summary for a run.",
    mime_type="application/json",
)
def run_summary_resource(run_id: str) -> str:
    return _dump(build_latest_run_events_summary(run_id))


@mcp.resource(
    "orcho://runs/{run_id}/parsed_plan.json",
    name="orcho_run_parsed_plan",
    description="Durable parsed_plan.json artifact for a run.",
    mime_type="application/json",
)
def run_parsed_plan_resource(run_id: str) -> str:
    return _dump(get_run_parsed_plan_raw(run_id))


@mcp.resource(
    "orcho://runs/{run_id}/evidence",
    name="orcho_run_evidence_bundle",
    description="Composed evidence bundle body for a run.",
    mime_type="application/json",
)
def run_evidence_resource(run_id: str) -> str:
    return _dump(get_run_evidence_raw(run_id))


@mcp.resource(
    "orcho://runs/{run_id}/diff.patch",
    name="orcho_run_diff_patch",
    description="Captured diff.patch artifact for a run.",
    mime_type="text/x-patch",
)
def run_diff_patch_resource(run_id: str) -> str:
    return get_run_diff_patch(run_id)


@mcp.resource(
    "orcho://runs/{run_id}/phases/{phase}/diff.patch",
    name="orcho_run_phase_diff_patch",
    description="Captured per-phase diff.patch artifact for a run phase.",
    mime_type="text/x-patch",
)
def run_phase_diff_patch_resource(run_id: str, phase: str) -> str:
    return get_run_phase_diff_patch(run_id, phase)


__all__ = [
    "run_diff_patch_resource",
    "run_evidence_resource",
    "run_events_resource",
    "run_meta_resource",
    "run_metrics_resource",
    "run_parsed_plan_resource",
    "run_phase_diff_patch_resource",
    "run_summary_resource",
    "runs_resource",
]
