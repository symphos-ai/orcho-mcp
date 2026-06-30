"""Lifecycle smoke matrix — four canonical scenarios in one readable file.

Each scenario is a single async test (~20 lines) that drives the run
through the MCP tool surface and asserts the terminal / paused state
plus one or two cardinal meta fields. The point is the readable
side-by-side trace per scenario, not exhaustive coverage:

  Scenario 1 — small_task (full_cycle) mock run completes cleanly.
  Scenario 2 — Plan reject pauses on phase handoff. [net new path]
  Scenario 3 — Decision (continue) + resume → completion.
  Scenario 4 — Hard cancel → terminal non-done with halt_reason.
  Scenario 5 — Decision (continue_with_waiver) + resume advances the
               run past the pause with a durable waiver recorded.
  Scenario 6 — planning (focused) mock run is plan-only and pauses on
               the planning handoff. [semantic focused coverage]

Scenarios 1 and 6 give the two mandatory Stage C semantic coverage
points: one full-cycle profile (``small_task`` / ``feature``) and one
focused profile (``planning``, ``recipe_kind=focused``) driven through
the MCP surface.

Scenarios 1, 3, 4 have parallel tests elsewhere in
``tests/acceptance/mock_pipeline/``; they earn their place here by
being side-by-side in one matrix the way an operator would read
them. Scenario 2 (``mock_validate_plan_reject=99`` triggering
``awaiting_phase_handoff``) is the only genuinely missing E2E path
the existing suite did not cover as a single deterministic check.

Gated by ``@pytest.mark.mcp_integration`` — runs only under
``pytest -m mcp_integration``.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

pytestmark = pytest.mark.mcp_integration

_TERMINAL_OR_PAUSED = {
    "done", "failed", "halted", "interrupted",
    "awaiting_phase_handoff", "orphaned",
}


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


async def _wait_pid_dead(pid: int, *, timeout_s: float = 10.0) -> bool:
    """Poll ``os.kill(pid, 0)`` until ``ProcessLookupError`` fires."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # Pid recycled by another process we don't own — original child gone.
            return True
        await asyncio.sleep(0.1)
    return False


# ── Scenario 1 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_smoke_small_task_mock_run_completes(mock_project) -> None:
    """``profile="small_task"`` + ``mock=True`` + ``max_rounds=1`` → done.

    The leanest full-cycle happy path: plan → validate_plan → implement,
    no review/fix rounds, no human gates. If this scenario fails, the
    entire L4 surface is broken — start debugging here.
    """
    from orcho_mcp.tools import orcho_run_start, orcho_run_status

    started = await orcho_run_start(
        task="smoke small_task",
        project_dir=str(mock_project),
        profile="small_task",
        mock=True,
        max_rounds=1,
    )

    final = await _wait_status(started.run_id, {"done"})
    assert final == "done"
    snap = orcho_run_status(started.run_id)
    assert (snap.meta or {}).get("status") == "done"
    assert (snap.meta or {}).get("profile") == "small_task"


# ── Scenario 2 — net new ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_smoke_plan_reject_pauses_on_handoff(mock_project) -> None:
    """``mock_validate_plan_reject=99`` forces every plan round to
    reject; the ``feature`` profile's ``human_feedback_on_reject``
    handoff fires and the run lands on ``awaiting_phase_handoff``.

    Net new lifecycle coverage — existing acceptance tests exercise
    reject-then-resume chains but no test pins the
    "max rejections → pause" path in isolation as a single
    deterministic trace.

    Asserts:
    - status reaches ``awaiting_phase_handoff``;
    - ``meta.phase_handoff`` carries the handoff descriptor
      (``id`` + ``available_actions``);
    - the paused phase is ``validate_plan`` (the only phase that
      can reject under the ``feature`` profile).
    """
    from orcho_mcp.tools import orcho_run_start, orcho_run_status

    started = await orcho_run_start(
        task="smoke plan reject",
        project_dir=str(mock_project),
        profile="feature",
        mock=True,
        max_rounds=1,
        mock_validate_plan_reject=99,
    )

    final = await _wait_status(started.run_id, {"awaiting_phase_handoff"})
    assert final == "awaiting_phase_handoff"

    snap = orcho_run_status(started.run_id)
    handoff = (snap.meta or {}).get("phase_handoff")
    assert handoff, "expected meta.phase_handoff to be populated on pause"
    assert handoff.get("phase") == "validate_plan"
    assert handoff.get("id"), "handoff_id missing"
    available = set(handoff.get("available_actions") or [])
    assert "continue" in available, (
        f"expected ``continue`` in available_actions; got {sorted(available)}"
    )


# ── Scenario 3 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_smoke_decide_continue_then_resume_completes(mock_project) -> None:
    """Paused run + ``phase_handoff_decide(action="continue")`` +
    ``orcho_run_resume`` → done.

    The full pause / decision / resume chain in one readable trace.
    Existing resume tests assert the resume mechanics; this scenario
    asserts the operator-facing chain end-to-end.
    """
    from orcho_mcp.tools import (
        orcho_phase_handoff_decide,
        orcho_run_resume,
        orcho_run_start,
        orcho_run_status,
    )

    # 1. Spawn → reject loop → pause.
    started = await orcho_run_start(
        task="smoke decide continue",
        project_dir=str(mock_project),
        profile="feature",
        mock=True,
        max_rounds=1,
        mock_validate_plan_reject=3,
    )
    paused = await _wait_status(
        started.run_id, {"awaiting_phase_handoff"}, timeout_s=30.0,
    )
    assert paused == "awaiting_phase_handoff"

    snap = orcho_run_status(started.run_id)
    handoff_id = (snap.meta or {}).get("phase_handoff", {}).get("id")
    assert handoff_id, "handoff_id missing on the paused run"

    # 2. Decide continue (writes the decision artifact; does NOT spawn).
    await orcho_phase_handoff_decide(
        run_id=started.run_id,
        handoff_id=handoff_id,
        action="continue",
    )

    # 3. Resume — fresh subprocess loads the checkpoint and continues.
    await orcho_run_resume(started.run_id)

    final = await _wait_status(started.run_id, {"done"})
    assert final == "done"


# ── Scenario 4 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_smoke_hard_cancel_lands_terminal_with_halt_reason(
    mock_project,
) -> None:
    """``cancel(mode="hard")`` → SIGKILL → terminal status + ``halt_reason``.

    Pins the hard-cancel lifecycle: signal_sent(hard) immediately,
    the subprocess dies under SIGKILL, the supervisor's reap stamps
    ``halt_reason`` (signal:SIGKILL), and ``orcho_run_status`` surfaces
    the terminal state via the meta + supervisor merge.

    Asserts:
    - cancel returns ``signal_sent(hard)``;
    - pid eventually dies;
    - final status is non-done terminal (``failed`` / ``interrupted`` /
      ``halted``) — the exact label depends on whether the pipeline
      caught the signal cleanly before SIGKILL fired;
    - ``halt_reason`` is populated (the cancel-race fix from earlier
      guarantees this on the supervisor side regardless of meta state).
    """
    from orcho_mcp.tools import orcho_run_cancel, orcho_run_start, orcho_run_status

    started = await orcho_run_start(
        task="smoke hard cancel",
        project_dir=str(mock_project),
        profile="feature",
        mock=True,
        max_rounds=1,
        mock_validate_plan_reject=3,  # keep the pipeline busy a moment
    )

    # Brief delay so the subprocess has actually started — cancel of a
    # not-yet-spawned run would race the spawn handshake.
    await asyncio.sleep(0.1)

    result = await orcho_run_cancel(started.run_id, mode="hard")
    assert result.status in {"signal_sent(hard)", "already_done", "already_dead"}, (
        f"unexpected cancel result: {result.status!r}"
    )

    if result.status == "signal_sent(hard)":
        assert await _wait_pid_dead(started.pid), (
            f"pid {started.pid} did not die under SIGKILL"
        )

    final = await _wait_status(started.run_id, _TERMINAL_OR_PAUSED)
    # Hard cancel during plan loop never reaches done.
    assert final != "done", (
        f"hard cancel reached ``done`` status — race with completion? "
        f"final={final!r}"
    )

    snap = orcho_run_status(started.run_id)
    meta = snap.meta or {}
    halt_reason = meta.get("halt_reason")
    assert halt_reason, (
        "halt_reason missing after hard cancel — the cancel-race fix "
        "guarantees the supervisor stamps it even when meta lags."
    )


# ── Scenario 5 — net new (continue_with_waiver) ─────────────────────────────


@pytest.mark.asyncio
async def test_smoke_decide_waiver_then_resume_advances(mock_project) -> None:
    """Paused run + ``phase_handoff_decide(action="continue_with_waiver",
    feedback=...)`` + ``orcho_run_resume`` → run advances past the pause
    with a durable operator waiver recorded.

    The waiver path is the operator accepting a rejected verdict as-is
    while recording why. Asserts the MCP-layer consumer contract:

    - the rejected handoff actually offers ``continue_with_waiver``;
    - the decision round-trips the feedback into the persisted artifact
      and advertises a single non-optional ``orcho_run_resume`` followup;
    - resume is honoured end-to-end: the run leaves
      ``awaiting_phase_handoff`` (the waived plan-loop findings are not
      reopened as a fresh pause) and reaches ``done``;
    - the durable ``phase_handoff_waiver`` lands in meta so a
      fresh-process downstream gate injects it rather than re-litigating
      the waived findings.

    The run reaches strict ``done``: stripping the rejected plan loop on
    resume rehydrates the persisted ``parsed_plan.json`` into state, so
    subtask_dag ``implement`` runs the waived plan instead of halting with
    "requires a parsed plan" (the historical pure-mock halt — see the
    ``parsed_plan`` resume-rehydration fix in
    ``orcho-core`` ``pipeline.project.handoff``).
    """
    from orcho_mcp.tools import (
        orcho_phase_handoff_decide,
        orcho_run_resume,
        orcho_run_start,
        orcho_run_status,
    )

    started = await orcho_run_start(
        task="smoke decide waiver",
        project_dir=str(mock_project),
        profile="feature",
        mock=True,
        max_rounds=1,
        mock_validate_plan_reject=3,
    )
    paused = await _wait_status(
        started.run_id, {"awaiting_phase_handoff"}, timeout_s=30.0,
    )
    assert paused == "awaiting_phase_handoff"

    snap = orcho_run_status(started.run_id)
    handoff = (snap.meta or {}).get("phase_handoff", {})
    handoff_id = handoff.get("id")
    assert handoff_id, "handoff_id missing on the paused run"
    available = set(handoff.get("available_actions") or [])
    assert "continue_with_waiver" in available, (
        "rejected handoff must offer continue_with_waiver; got "
        f"{sorted(available)}"
    )

    waiver_text = "F1 is a known false positive on mock fixtures; accepted."
    decided = await orcho_phase_handoff_decide(
        run_id=started.run_id,
        handoff_id=handoff_id,
        action="continue_with_waiver",
        feedback=waiver_text,
    )
    # Decision round-trips the waiver text into the persisted artifact.
    assert decided.action == "continue_with_waiver"
    assert decided.feedback == waiver_text
    # Single non-optional resume followup is advertised.
    resume_actions = [a for a in decided.next_actions if a.tool == "orcho_run_resume"]
    assert resume_actions and resume_actions[0].optional is False

    await orcho_run_resume(started.run_id)

    # Resume is honoured end-to-end: the run advances past the pause and
    # completes. It must NOT re-pause on the waived findings, and the
    # waived plan must carry into implement (no missing-plan halt).
    final = await _wait_status(started.run_id, {"done"})
    assert final == "done"

    snap = orcho_run_status(started.run_id)
    meta = snap.meta or {}
    assert meta.get("phase_handoff") is None, (
        "active handoff payload must clear after the waiver decision + resume"
    )
    waiver = meta.get("phase_handoff_waiver")
    assert waiver, (
        "continue_with_waiver must persist a durable phase_handoff_waiver "
        "into meta so downstream gates inject it"
    )
    assert waiver.get("waiver_text") == waiver_text


# ── Scenario 6 — focused semantic profile (planning) ────────────────────────


@pytest.mark.asyncio
async def test_smoke_planning_focused_run_is_plan_only(mock_project) -> None:
    """``profile="planning"`` (``recipe_kind=focused``) drives a plan-only
    run through MCP that pauses on the planning handoff.

    This is the mandatory focused-profile coverage point. A focused
    ``planning`` recipe runs the plan / validate_plan block and stops —
    there is no implement / review / fix work — so under ``mock=True`` it
    lands on ``awaiting_phase_handoff`` for operator plan review.

    Asserts:
    - the run reaches ``awaiting_phase_handoff`` (plan-only, no
      implementation phase to carry it to ``done``);
    - ``meta.profile == "planning"`` — the semantic focused profile
      threaded through the MCP start surface;
    - no ``implement`` phase ran (the recipe is genuinely plan-only).
    """
    from orcho_mcp.tools import orcho_run_start, orcho_run_status

    started = await orcho_run_start(
        task="smoke planning focused",
        project_dir=str(mock_project),
        profile="planning",
        mock=True,
        max_rounds=1,
    )

    final = await _wait_status(
        started.run_id, {"awaiting_phase_handoff", "done"},
    )
    assert final == "awaiting_phase_handoff", (
        f"planning is plan-only and should pause for plan review; got {final!r}"
    )

    snap = orcho_run_status(started.run_id)
    meta = snap.meta or {}
    assert meta.get("profile") == "planning", (
        f"focused smoke must thread the planning profile; got "
        f"{meta.get('profile')!r}"
    )
    phases = meta.get("phases", {})
    assert "implement" not in phases, (
        f"planning is focused / plan-only — no implement phase expected; "
        f"got {sorted(phases)}"
    )
