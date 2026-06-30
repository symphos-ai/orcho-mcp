"""orcho_mcp.resources — read-only artefacts published under ``orcho://*`` URIs.

Resources let MCP clients pull artefacts lazily by URI rather than copying
JSON blobs into tool arguments. Same data the read tools surface, but
addressable: ``orcho://runs/<id>/meta`` is cheaper for a client to refresh
than calling ``orcho_run_status`` and getting the whole snapshot.

URI scheme:
  orcho://workspace                            — workspace info
  orcho://runs                                 — recent runs (newest first)
  orcho://runs/{run_id}/meta                   — meta.json
  orcho://runs/{run_id}/metrics                — metrics.json
  orcho://runs/{run_id}/events                 — full events.jsonl (NDJSON)
  orcho://runs/{run_id}/summary                — latest bounded event summary
  orcho://runs/{run_id}/parsed_plan.json       — durable parsed plan artifact
  orcho://runs/{run_id}/evidence               — composed evidence bundle
  orcho://runs/{run_id}/diff.patch             — captured unified diff
  orcho://profiles                             — profile catalogue
  orcho://profiles/{name}                      — single profile
  orcho://projects/{project_b64}/skills        — skills under .agent/multiagent/skills/
  orcho://workflows                            — workflow recipe catalogue

``project_b64`` is a URL-safe base64 of the project_dir absolute path; raw
paths can't be embedded in URI segments. Helper ``encode_project_dir`` in
this package mirrors the encoding for callers that need to construct the URI.

Same stdio-purity invariant as tools: never ``print()``. Errors raise
``orcho_mcp.errors`` subclasses; FastMCP turns them into JSON-RPC errors.

Package layout: each domain (workspace, runs, profiles, projects) lives
in its own submodule. The ``__init__`` imports them all for ``@mcp.resource``
decorator side-effects and re-exports the public surface for callers that
do ``from orcho_mcp.resources import …``.
"""
from __future__ import annotations

from orcho_mcp.resources.helpers import (
    decode_project_dir,
    encode_project_dir,
)
from orcho_mcp.resources.profiles import (
    profile_resource,
    profiles_resource,
)
from orcho_mcp.resources.projects import project_skills_resource
from orcho_mcp.resources.runs import (
    run_diff_patch_resource,
    run_events_resource,
    run_evidence_resource,
    run_meta_resource,
    run_metrics_resource,
    run_parsed_plan_resource,
    run_phase_diff_patch_resource,
    run_summary_resource,
    runs_resource,
)
from orcho_mcp.resources.workflows import workflows_resource
from orcho_mcp.resources.workspace import workspace_resource

__all__ = [
    "decode_project_dir",
    "encode_project_dir",
    "profile_resource",
    "profiles_resource",
    "project_skills_resource",
    "run_diff_patch_resource",
    "run_evidence_resource",
    "run_events_resource",
    "run_meta_resource",
    "run_metrics_resource",
    "run_parsed_plan_resource",
    "run_phase_diff_patch_resource",
    "run_summary_resource",
    "runs_resource",
    "workflows_resource",
    "workspace_resource",
]
