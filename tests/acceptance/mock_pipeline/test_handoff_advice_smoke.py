"""L4 mock-pipeline smoke for the handoff-advice surface (cross-repo rule).

Two scenarios, both driven through the MCP tool surface against a real
supervisor-spawned ``--mock`` pipeline (zero real API calls):

  Scenario 1 — the mandatory cross-repo smoke: ``orcho_run_start(mock=True,
               max_rounds=1)`` → ``orcho_run_status`` reaches ``done`` and the
               persisted session carries the expected shape.
  Scenario 2 — the full live trace on a REAL supervisor-spawned mock run: drive
               a ``feature`` mock run to a paused rejected handoff
               (``mock_validate_plan_reject=99``), call ``orcho_handoff_advice``
               on that paused handoff, assert the typed ``retry_feedback``
               ``ready_next_action`` (provenance ``args.note == provenance_note``),
               then read ``orcho_run_evidence(slice="handoff_advice")`` and
               confirm a durable advice call appears for that handoff.

Scenario 2 is HERMETIC: the run is started with ``mock=True``, so the supervisor
state records ``mock`` and the MCP advisor boundary recovers a
``MockAgentProvider`` (see
``orcho_mcp.run_control.advice._resolve_advisor_provider``). The in-process
advisor pass therefore runs deterministically with ZERO real-provider calls —
the same seam the L3 stdio success path relies on. This exercises the highest-
risk path end-to-end: real supervisor-spawned mock paused handoff → in-process
advisor → durable advice artifact → evidence projection.

Gated by ``@pytest.mark.mcp_integration`` — runs only under
``pytest -m mcp_integration``.
"""
from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = pytest.mark.mcp_integration


async def _wait_status(
    run_id: str,
    accept: set[str],
    *,
    timeout_s: float = 60.0,
    poll_s: float = 0.2,
) -> str:
    """Poll ``orcho_run_status`` until ``meta.status`` lands in ``accept``."""
    from orcho_mcp.tools import orcho_run_status

    deadline = time.monotonic() + timeout_s
    last_seen: str | None = None
    while time.monotonic() < deadline:
        snap = orcho_run_status(run_id)
        cur = (snap.meta or {}).get("status")
        last_seen = cur
        if cur in accept:
            return cur
        await asyncio.sleep(poll_s)
    raise AssertionError(
        f"run {run_id} never reached one of {sorted(accept)} within "
        f"{timeout_s}s (last seen: {last_seen!r})"
    )


# ── Scenario 1 — mandatory cross-repo smoke (start → status) ────────────────


@pytest.mark.asyncio
async def test_handoff_advice_mock_smoke_start_status(mock_project) -> None:
    """``orcho_run_start(mock=True, max_rounds=1)`` → ``orcho_run_status`` done.

    The mandatory minimum smoke for the cross-repo validation rule: a mock run
    completes and the persisted session shape is readable through the MCP read
    surface.
    """
    from orcho_mcp.tools import orcho_run_start, orcho_run_status

    started = await orcho_run_start(
        task="handoff-advice smoke",
        project_dir=str(mock_project),
        profile="small_task",
        mock=True,
        max_rounds=1,
    )

    final = await _wait_status(started.run_id, {"done"})
    assert final == "done"

    snap = orcho_run_status(started.run_id)
    meta = snap.meta or {}
    assert meta.get("status") == "done"
    assert meta.get("profile") == "small_task"
    assert snap.run_id == started.run_id


# ── Scenario 2 — live paused → advice → evidence on a real mock run ─────────


@pytest.mark.asyncio
async def test_paused_run_handoff_advice_live_trace(mock_project) -> None:
    """Live ``paused → orcho_handoff_advice → evidence`` on a real mock run.

    Drives the ``feature`` profile to ``awaiting_phase_handoff`` via forced plan
    rejection, then exercises the full advisor trace on that genuine,
    supervisor-spawned mock run:

      1. ``orcho_handoff_advice`` produces a typed recommendation. Because the
         run was started ``mock=True``, the advisor resolves a
         ``MockAgentProvider`` in-process (no real API call) and returns a
         confident ``retry_feedback`` recommendation.
      2. ``ready_next_action`` is the pre-filled call to the EXISTING
         ``orcho_phase_handoff_decide`` retry verb carrying mandatory
         provenance (``args.note == provenance_note``) — the acceptance
         criterion the prior empty-slice assertion could not reach.
      3. ``orcho_run_evidence(slice="handoff_advice")`` now reports the durable
         advice call written by that pass, keyed to the same handoff.
    """
    from orcho_mcp.tools import (
        orcho_handoff_advice,
        orcho_run_evidence,
        orcho_run_start,
        orcho_run_status,
    )

    started = await orcho_run_start(
        task="handoff-advice paused live trace",
        project_dir=str(mock_project),
        profile="feature",
        mock=True,
        max_rounds=1,
        mock_validate_plan_reject=99,
    )

    paused = await _wait_status(
        started.run_id, {"awaiting_phase_handoff"}, timeout_s=30.0,
    )
    assert paused == "awaiting_phase_handoff"

    # The active paused handoff id — the advice call (and its evidence) keys to it.
    snap = orcho_run_status(started.run_id)
    handoff = (snap.meta or {}).get("phase_handoff") or {}
    handoff_id = handoff.get("id")
    assert handoff_id, "paused run must expose an active phase_handoff id"

    # (1)+(2) — typed advice with a provenance-bearing retry ready_next_action.
    advice = orcho_handoff_advice(started.run_id)
    assert advice.run_id == started.run_id
    assert advice.handoff_id == handoff_id
    assert advice.recommended_action == "retry_feedback"
    assert advice.retry_feedback
    assert advice.advice_artifact.startswith("phase_handoff_advice/")
    assert advice.provenance_note

    ready = advice.ready_next_action
    assert ready is not None
    assert ready.tool == "orcho_phase_handoff_decide"
    assert ready.args["action"] == "retry_feedback"
    assert ready.args["feedback"] == advice.retry_feedback
    assert ready.args["note"] == advice.provenance_note
    assert ready.args["note"], "ready_next_action.args.note must be non-empty"

    # (3) — the durable advice call now surfaces in the evidence slice.
    ev = orcho_run_evidence(started.run_id, slice="handoff_advice")
    assert ev.slice == "handoff_advice"
    assert ev.handoff_advice is not None
    assert ev.handoff_advice.summary.calls >= 1
    calls_for_handoff = [
        c for c in ev.handoff_advice.calls if c.handoff_id == handoff_id
    ]
    assert calls_for_handoff, (
        "evidence must carry the durable advice call for the paused handoff "
        f"{handoff_id!r}; saw {[c.handoff_id for c in ev.handoff_advice.calls]}"
    )
    call = calls_for_handoff[0]
    assert call.recommended_action == "retry_feedback"
    assert call.advice_artifact == advice.advice_artifact
