"""orcho_mcp.services.run_artifacts — raw per-run artifact reads.

Sister to ``services.run_reads``. ``run_reads`` returns Pydantic models
shaped for the MCP tool wire (``RunStatus`` / ``RunMetrics`` /
``EventsTailResult``); this module returns the raw artifact bodies
(``dict`` / NDJSON ``str``) for the resource layer, which serves
``orcho://runs/{id}/...`` URIs as static JSON / JSONL blobs without
tool framing.

Keeping the two surfaces separate means the resource layer never
imports the SDK directly and never reads run files directly — every
artifact crossing the MCP boundary flows through one of these helpers.
"""
from __future__ import annotations

import json
import re

from sdk import (
    collect_evidence as _sdk_collect_evidence,
    get_run_diff as _sdk_get_run_diff,
    get_run_metrics as _sdk_get_run_metrics,
    load_meta as _sdk_load_meta,
)

from orcho_mcp.errors import RunNotFoundError
from orcho_mcp.services.errors import map_sdk_errors
from orcho_mcp.services.run_lookup import find_run_dir


def get_run_meta_raw(run_id: str) -> dict:
    """Return the raw ``meta.json`` dict for a run.

    Resolves the run dir via ``find_run_dir`` (which raises
    ``RunNotFoundError`` for unknown ids) and delegates the file read
    to the SDK so file-shape edge cases (atomic-write, missing fields)
    are handled in one place.
    """
    run_dir = find_run_dir(run_id)
    return _sdk_load_meta(run_dir)


def get_run_metrics_raw(run_id: str) -> dict:
    """Return the raw ``metrics.json`` dict for a run.

    Raises ``RunNotFoundError`` when the run is unknown or has no
    ``metrics.json`` on disk. Cross-runs reach a terminal without a
    ``metrics.json`` only when no per-project or cross-level usage was
    captured (rare); the resource treats that as "no metrics
    available" identically to a missing run.
    """
    with map_sdk_errors(run_id):
        m = _sdk_get_run_metrics(run_id, cwd=None)
    if not m.raw:
        raise RunNotFoundError(f"no metrics.json for run {run_id}")
    return m.raw


def get_run_parsed_plan_raw(run_id: str) -> dict:
    """Return the durable ``parsed_plan.json`` artifact for a run."""
    run_dir = find_run_dir(run_id)
    path = run_dir / "parsed_plan.json"
    if not path.is_file():
        raise RunNotFoundError(f"no parsed_plan.json for run {run_id}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RunNotFoundError(
            f"invalid parsed_plan.json for run {run_id}: {e}"
        ) from e


# orcho-core writes the per-run delivery audit at
# ``<run_dir>/commit_decisions/<decision_id>.json`` where ``decision_id`` is
# the sanitized run id (``pipeline.engine.commit_delivery._safe_decision_id``
# / ``_artifact_path``). The sanitizer is core-internal and not re-exported by
# the SDK (T1 audit), so the id transform is replicated here verbatim. This is
# a thin durable-artifact read only — MCP never re-implements delivery.
_SAFE_DECISION_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_decision_id(run_id: str) -> str:
    """Sanitize ``run_id`` to the commit-decision artifact stem.

    Mirrors orcho-core's ``_safe_decision_id`` so the artifact filename
    matches what the engine wrote.
    """
    return _SAFE_DECISION_ID_RE.sub("_", run_id).strip("._")


def get_run_commit_decision_raw(run_id: str) -> dict:
    """Return the durable ``commit_decisions/<id>.json`` audit artifact.

    Reads the per-run commit-delivery decision audit the engine persists
    under ``<run_dir>/commit_decisions/``. Resolves the canonical
    ``<safe_run_id>.json`` first and falls back to the sole ``*.json`` in
    the directory if the exact stem is absent (defensive against id-sanitize
    skew). Raises ``RunNotFoundError`` when the run is unknown, the directory
    or file is missing, or the JSON is unreadable — callers in the delivery
    gate treat that as "secondary artifact unavailable" and degrade the diff
    summary without hiding the gate.
    """
    run_dir = find_run_dir(run_id)
    decisions_dir = run_dir / "commit_decisions"
    path = decisions_dir / f"{_safe_decision_id(run_id)}.json"
    if not path.is_file() and decisions_dir.is_dir():
        candidates = sorted(decisions_dir.glob("*.json"))
        if len(candidates) == 1:
            path = candidates[0]
    if not path.is_file():
        raise RunNotFoundError(
            f"no commit_decisions artifact for run {run_id}",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RunNotFoundError(
            f"invalid commit_decisions artifact for run {run_id}: {e}",
        ) from e
    if not isinstance(data, dict):
        raise RunNotFoundError(
            f"malformed commit_decisions artifact for run {run_id}",
        )
    return data


def get_run_evidence_raw(run_id: str) -> dict:
    """Return the composed evidence bundle body for a run."""
    with map_sdk_errors(run_id):
        bundle = _sdk_collect_evidence(run_id, cwd=None)
    return bundle.body


def get_run_diff_patch(run_id: str) -> str:
    """Return the captured ``diff.patch`` artifact as raw unified diff."""
    with map_sdk_errors(run_id):
        diff = _sdk_get_run_diff(run_id, cwd=None, mode="full", color=False)
    if not diff.found:
        raise RunNotFoundError(f"no diff.patch for run {run_id}")
    return diff.content


def get_run_phase_diff_patch(run_id: str, phase: str) -> str:
    """Return the captured per-phase ``diff.patch`` as raw unified diff.

    Mirrors :func:`get_run_diff_patch` for the per-phase artifact at
    ``<run_dir>/phases/<phase>/diff.patch``. Malformed phase values
    surface as ``InvalidPlanError`` (via the SDK's ``ValueError``); a
    valid-but-missing phase artifact surfaces as ``RunNotFoundError``
    to match the existing run-level resource contract — resources
    raise, they don't return soft ``found=False`` payloads.
    """
    with map_sdk_errors(run_id):
        diff = _sdk_get_run_diff(
            run_id, cwd=None, mode="full", color=False, phase=phase,
        )
    if not diff.found:
        raise RunNotFoundError(
            f"no diff.patch for phase {phase!r} of run {run_id}",
        )
    return diff.content


def get_run_events_ndjson(run_id: str) -> str:
    """Return the full ``events.jsonl`` stream as a single string.

    Returns an empty string if the run exists but has not emitted any
    events yet (e.g. crashed before the first phase banner). The resource
    surfaces the raw newline-delimited JSON without re-framing — clients
    decode line-by-line.
    """
    run_dir = find_run_dir(run_id)
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        return ""
    return events_path.read_text(encoding="utf-8")


__all__ = [
    "get_run_commit_decision_raw",
    "get_run_diff_patch",
    "get_run_evidence_raw",
    "get_run_events_ndjson",
    "get_run_meta_raw",
    "get_run_metrics_raw",
    "get_run_parsed_plan_raw",
    "get_run_phase_diff_patch",
]
