"""orcho_mcp.services.meta_summary — bounded projection of run meta.json.

``orcho_run_status`` is the supervisor's highest-frequency poll: it is
called repeatedly to watch a run's progress, and each call's payload
lands directly in the operator LLM's context window. The raw
``meta.json`` embeds full phase *bodies* — the plan markdown for every
replan round, the implement agent's final output, the validate_plan /
repair critiques, and the per-subtask implementation receipts. On a real
run these dominated the payload (~130k of ~150k characters were phase
bodies); the actual status / metrics / lineage information the poller
needs was under 5%.

This module projects ``meta`` into a *summary-only* shape:

- top-level scalars and gate results (``status``, ``halt_reason``,
  ``worktree``, ``pending_gate``, ``contract_check`` verdicts, …) pass
  through untouched;
- the long task text is truncated to a short summary plus a
  ``task_chars`` size marker;
- inside ``phases``, heavy string bodies become ``<key>_chars`` size
  markers, file-path / findings lists become ``<key>_count`` integers,
  implementation receipts collapse to ``subtask_id`` + ``state`` (plus a
  few cheap scalars), and per-attempt observability dicts
  (``prompt_render``, ``context_*``) are dropped.

The full bodies stay on disk under ``run_dir`` and remain reachable
through ``orcho_run_evidence`` / ``orcho_run_metrics`` / a direct read of
``runspace/runs/<id>/``.

The ``include`` set re-admits specific body families for callers that
explicitly want them; ``"all"`` reproduces the pre-summary payload
verbatim (back-compat escape hatch). Recognised tokens:

- ``"task"``      — keep the full task text.
- ``"plan"``      — keep plan-phase markdown bodies + file lists.
- ``"output"``    — keep the implement phase's agent output body.
- ``"critiques"`` — keep validate_plan / repair / final_acceptance
  critique + raw-response bodies and their findings lists.
- ``"receipts"``  — keep full implementation / repair receipts.
- ``"all"``       — keep everything (identity projection).

Defensive contract: this is a pure function with no SDK / IO. A corrupt
or partial ``meta`` must never raise — non-dict inputs and missing keys
coerce to safe defaults, mirroring the rest of the projection surface.
"""
from __future__ import annotations

import json
from typing import Any

# First N characters of the task text kept inline as a short summary.
_TASK_SUMMARY_MAX = 280

# Heavy free-text body keys: replaced by ``<key>_chars`` unless their
# owning phase keeps bodies. ``output`` covers both the plan markdown and
# the implement agent output — which token controls it is decided
# per-phase via the ``keep_bodies`` flag, not by the key name.
_BODY_STR_KEYS = frozenset(
    {"output", "critique", "raw_response", "repair_output", "replan_critique"},
)

# List bodies summarised to ``<key>_count`` unless kept.
_LIST_COUNT_KEYS = frozenset(
    {"findings", "parsed_file_paths", "existing_files", "missing_files"},
)

# Per-subtask receipt collections, summarised unless ``receipts`` kept.
_RECEIPT_LIST_KEYS = frozenset({"implementation_receipts"})
_RECEIPT_DICT_KEYS = frozenset({"repair_receipt"})

# Cheap scalar fields kept when a receipt is summarised; the heavy
# ``done_criteria`` / ``criteria_report`` / ``attestation_summary`` bodies
# are dropped (reachable via ``orcho_run_evidence``).
_RECEIPT_KEEP = (
    "subtask_id",
    "state",
    "runtime",
    "model",
    "skill",
    "depends_on",
    "duration",
)

# Per-attempt observability dicts — never status-relevant, always dropped.
_OBS_PREFIXES = (
    "prompt_render",
    "context_growth",
    "context_clearing",
    "context_pressure",
)

_INCLUDE_ALL = "all"


def summarize_run_meta(
    meta: dict[str, Any],
    *,
    include: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Project ``meta`` into a bounded, summary-only wire shape.

    ``include`` re-admits body families (see module docstring). Returns a
    new dict; the input is never mutated. Non-dict input is returned
    unchanged so a corrupt payload can never raise here.
    """
    if not isinstance(meta, dict):
        return meta

    inc = {str(t).lower() for t in include}
    if _INCLUDE_ALL in inc:
        return meta

    out = dict(meta)

    if "task" not in inc:
        task = out.get("task")
        if isinstance(task, str) and len(task) > _TASK_SUMMARY_MAX:
            out["task"] = task[:_TASK_SUMMARY_MAX]
            out["task_chars"] = len(task)
            out["task_truncated"] = True

    phases = out.get("phases")
    if isinstance(phases, dict):
        out["phases"] = {
            name: _summarize_phase(name, value, inc)
            for name, value in phases.items()
        }

    return out


def _summarize_phase(name: str, value: Any, inc: set[str]) -> Any:
    """Dispatch a single ``phases.<name>`` entry to its summariser."""
    if name == "plan" and isinstance(value, list):
        keep = "plan" in inc
        return [
            _project_entry(e, keep_bodies=keep, keep_lists=keep, keep_receipts=False)
            for e in value
        ]
    if name == "validate_plan" and isinstance(value, list):
        keep = "critiques" in inc
        return [
            _project_entry(e, keep_bodies=keep, keep_lists=keep, keep_receipts=False)
            for e in value
        ]
    if name == "rounds" and isinstance(value, list):
        keep = "critiques" in inc
        return [
            _project_entry(
                e,
                keep_bodies=keep,
                keep_lists=keep,
                keep_receipts="receipts" in inc,
            )
            for e in value
        ]
    if name == "implement" and isinstance(value, dict):
        return _project_entry(
            value,
            keep_bodies="output" in inc,
            keep_lists=True,
            keep_receipts="receipts" in inc,
        )
    if name == "final_acceptance" and isinstance(value, dict):
        keep = "critiques" in inc
        return _project_entry(
            value, keep_bodies=keep, keep_lists=keep, keep_receipts=False,
        )
    return _summarize_unknown(value, inc)


def _summarize_unknown(value: Any, inc: set[str]) -> Any:
    """Light-touch summary for gate / unknown phases.

    Handles the alias-keyed gate shape (``{"api": {...}}`` for
    ``contract_check``) by recursing one level, and bare entry dicts /
    lists otherwise. Heavy bodies are elided under the general
    ``critiques`` / ``receipts`` tokens.
    """
    keep_bodies = "critiques" in inc
    keep_lists = "critiques" in inc
    keep_receipts = "receipts" in inc
    if isinstance(value, dict):
        if value and all(isinstance(v, dict) for v in value.values()):
            return {
                alias: _project_entry(
                    entry,
                    keep_bodies=keep_bodies,
                    keep_lists=keep_lists,
                    keep_receipts=keep_receipts,
                )
                for alias, entry in value.items()
            }
        return _project_entry(
            value,
            keep_bodies=keep_bodies,
            keep_lists=keep_lists,
            keep_receipts=keep_receipts,
        )
    if isinstance(value, list):
        return [
            _project_entry(
                e,
                keep_bodies=keep_bodies,
                keep_lists=keep_lists,
                keep_receipts=keep_receipts,
            )
            for e in value
        ]
    return value


def _project_entry(
    entry: Any,
    *,
    keep_bodies: bool,
    keep_lists: bool,
    keep_receipts: bool,
) -> Any:
    """Copy one phase-attempt dict, eliding heavy fields unless kept."""
    if not isinstance(entry, dict):
        return entry
    out: dict[str, Any] = {}
    for key, val in entry.items():
        if key.startswith(_OBS_PREFIXES):
            continue
        if key in _BODY_STR_KEYS and not keep_bodies:
            out[f"{key}_chars"] = _charlen(val)
            continue
        if key in _LIST_COUNT_KEYS and not keep_lists:
            out[f"{key}_count"] = len(val) if isinstance(val, list) else 0
            continue
        if key in _RECEIPT_LIST_KEYS and not keep_receipts:
            out[key] = (
                [_summarize_receipt(r) for r in val]
                if isinstance(val, list)
                else val
            )
            continue
        if key in _RECEIPT_DICT_KEYS and not keep_receipts:
            out[key] = _summarize_receipt(val)
            continue
        out[key] = val
    return out


def _summarize_receipt(receipt: Any) -> Any:
    """Collapse a receipt to its identity + cheap scalar fields."""
    if not isinstance(receipt, dict):
        return receipt
    return {k: receipt[k] for k in _RECEIPT_KEEP if k in receipt}


def _charlen(value: Any) -> int:
    """Body size marker: string length, or JSON length for structures."""
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(str(value))


__all__ = ["summarize_run_meta"]
