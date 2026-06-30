"""orcho_mcp.services.status_merge — supervisor ↔ meta status reconciliation.

The pipeline owns ``meta.json``; supervisor cancellation paths (SIGKILL
always, SIGTERM before atexit) bypass its writer. When ``meta.status``
is missing / empty / stuck on ``running`` and the supervisor's state
file shows a terminal status, the supervisor wins. Otherwise
``meta.status`` is authoritative.

Centralised so ``orcho_run_status`` (tools.py) and
``build_run_events_summary`` (observe/summary.py) share one
implementation — without this helper the merge rule would drift the
next time someone touches supervisor → meta status reconciliation.

Pure module: no SDK imports, no IO except reading
``mcp_supervisor.json`` (an MCP-private contract that orcho-core's SDK
intentionally does not surface).

This is the status/halt-reason half of the single projection surface:
``merged_status_from_meta`` / ``merged_halt_reason_from_meta`` are
re-exported by :mod:`orcho_mcp.services.run_projection` (the projection
owner), and read paths import them from there. The implementation stays
here because it is pure and SDK-free; ``run_projection`` is where Stage
7C grows the projected read-model.
"""
from __future__ import annotations

import json
from pathlib import Path


def merged_status_from_meta(meta: dict, run_dir: Path) -> str | None:
    """Return the resolved run status — the value callers should display.

    Args:
        meta: already-loaded meta dict (caller does the IO once).
        run_dir: run directory for the supervisor state file probe.

    Returns:
        Resolved status string, or ``None`` when neither side has a
        non-empty status (callers treat as "unknown").
    """
    meta_status = meta.get("status")
    # Defensive: meta.status is sometimes a non-string when the pipeline
    # crashes mid-write; coerce to string for the trivial-check, but
    # always return a string (or None) to the caller.
    if isinstance(meta_status, str) and meta_status not in ("", "running"):
        return meta_status
    sup_status = supervisor_terminal_status(run_dir)
    if sup_status is not None:
        return sup_status
    # Meta had something trivial (``""`` or ``"running"``) and supervisor
    # is silent — surface the meta value as-is so callers see a stable
    # "still running" answer instead of None.
    if isinstance(meta_status, str) and meta_status:
        return meta_status
    return None


def supervisor_terminal_status(run_dir: Path) -> str | None:
    """Read ``mcp_supervisor.json`` and return its terminal status if any.

    Returns:
        - The supervisor's status string when terminal (``done`` /
          ``failed`` / ``interrupted`` / ``awaiting_phase_handoff`` /
          ``awaiting_gate_decision`` / ``orphaned``) plus the
          signal-induced ``failed`` rc<0 case remapped to
          ``interrupted`` so the wire vocabulary matches the lifecycle
          doc. ``awaiting_phase_handoff`` is emitted when a phase's
          declared handoff policy fires — resume via
          ``orcho_phase_handoff_decide`` + ``orcho_run_resume``.
          ``awaiting_gate_decision`` is emitted by the cross runner
          when a manual_confirm cross-gate has no operator override +
          no interactive transport; resume via ``orcho_run_resume``
          after the operator chooses run/skip.
        - ``None`` when the file is absent, unreadable, or holds a
          non-terminal status (``running`` — supervisor itself thinks
          the run is alive).

    Self-contained read: no SDK call, no MCP-level abstraction leak —
    this helper exists *because* the supervisor's state file is an
    MCP-private contract. The SDK doesn't surface it because it isn't
    part of the run-state model orcho-core owns.
    """
    state = _read_supervisor_state(run_dir)
    if state is None:
        return None
    sup_status = state.get("status")
    if sup_status in (None, "", "running"):
        return None
    if sup_status == "failed":
        # Signal-induced exits surface as ``interrupted`` per the
        # lifecycle doc; pipeline crashes (positive rc) stay ``failed``.
        rc = state.get("exit_code")
        if isinstance(rc, int) and rc < 0:
            return "interrupted"
    return sup_status


def merged_meta(meta: dict, run_dir: Path) -> dict:
    """Overlay the supervisor-merged ``status`` / ``halt_reason`` onto ``meta``.

    Returns a shallow copy of ``meta`` with ``status`` and ``halt_reason``
    replaced by their supervisor-reconciled values. This is the shared seam the
    core recovery-lineage / run-diagnosis read-models are fed: core echoes
    ``meta['status']`` / ``meta['halt_reason']`` verbatim, so a caller must hand
    it the merged values (not the raw on-disk ``meta.status`` that a SIGKILL may
    have left stuck on ``running``) or the projection loses the supervisor merge.

    Pure: the caller owns the ``meta`` / ``run_dir`` IO; this only reconciles the
    two already-readable fields, so it is safe to reuse for both the inspected run
    and each source candidate.
    """
    return {
        **meta,
        "status": merged_status_from_meta(meta, run_dir),
        "halt_reason": merged_halt_reason_from_meta(meta, run_dir),
    }


def merged_halt_reason_from_meta(meta: dict, run_dir: Path) -> str | None:
    """Return the resolved ``halt_reason`` for the run.

    The pipeline owns ``meta.halt_reason`` (set by finalize on the
    declared halt path, by ``_record_phase_failure`` on the failed
    path, or by the atexit hook as a last-ditch ``"interrupted"``).
    When that field is absent — typically because SIGKILL bypassed
    every in-process writer — the supervisor's reap-time taxonomy
    (``signal:<NAME>`` / ``abnormal_exit:<rc>`` /
    ``interrupted_orphan`` / ``orphaned_no_supervisor``) fills in.

    Returns:
        The resolved reason string, or ``None`` when neither side has
        anything to say (callers display nothing).
    """
    meta_reason = meta.get("halt_reason")
    if isinstance(meta_reason, str) and meta_reason:
        return meta_reason
    return supervisor_halt_reason(run_dir)


def supervisor_halt_reason(run_dir: Path) -> str | None:
    """Read ``mcp_supervisor.json`` and return its ``halt_reason`` if any.

    Self-contained read mirroring :func:`supervisor_terminal_status`
    so consumers can surface a reason for SIGKILL / orphan / abnormal
    exit cases that bypassed the pipeline's own writers.
    """
    state = _read_supervisor_state(run_dir)
    if state is None:
        return None
    reason = state.get("halt_reason")
    if isinstance(reason, str) and reason:
        return reason
    return None


def _read_supervisor_state(run_dir: Path) -> dict | None:
    """Load ``mcp_supervisor.json`` once; return ``None`` on any read error.

    Promoted into ``__all__`` so :mod:`orcho_mcp.services.run_control_boundary`
    can reuse the one canonical ``mcp_supervisor.json`` reader instead of
    re-implementing the file probe. Behavior is unchanged — this is the same
    self-contained read the status merge already relies on.
    """
    state_path = run_dir / "mcp_supervisor.json"
    if not state_path.is_file():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


__all__ = [
    "_read_supervisor_state",
    "merged_halt_reason_from_meta",
    "merged_meta",
    "merged_status_from_meta",
    "supervisor_halt_reason",
    "supervisor_terminal_status",
]
