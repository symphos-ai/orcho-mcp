"""``orcho_run_diagnose`` settles a run whose process is gone (J1).

An operator on 0.8.5 found four runs whose processes had been gone for
8-23 hours while every orcho surface — ``meta.json``, ``orcho status``, and
this tool — still described them as live work. Core's diagnosis has had a
dead-process branch all along; it could not fire, because the liveness
reader rejected the naive local timestamps the event writer actually emits
and aged every real run to ``UNKNOWN``.

This is the MCP end of that contract. The runs here are written the way a
real abandoned run looks — a launch artifact naming a dead pid, an event
stream in the writer's own format, no terminal event — so the wire answer
is pinned against the shape that was misreported, not an idealised one.

Requires the orcho-core fix (`fix(liveness): age the event stamps the
writer actually produces`); core merges before mcp.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orcho_mcp.inspection.diagnosis import inspect_run_diagnosis
from tests.fixtures.mcp_workspace import meta, supervisor_state, write_run

_DEAD_PID = 2_147_480_000  # far above any live pid; the probe answers "dead"


def _write_abandoned_run(
    workspace: Path,
    run_id: str,
    *,
    age_seconds: float,
    pid: int = _DEAD_PID,
    terminal_event: bool = False,
) -> Path:
    """A run whose record says ``running`` and whose process is gone."""
    launched = datetime.now(UTC) - timedelta(seconds=age_seconds)
    # The event writer's real format: naive local wall-clock, no offset.
    stamped = (datetime.now() - timedelta(seconds=age_seconds)).isoformat()
    events = [{
        "seq": 1, "ts": stamped, "kind": "agent.tool_use",
        "phase": "implement", "payload": {},
    }]
    if terminal_event:
        events.append({
            "seq": 2, "ts": stamped, "kind": "run.end",
            "phase": "implement", "payload": {},
        })
    run_dir = write_run(
        workspace, run_id,
        meta=meta(status="running", current_phase="implement"),
        events=events,
        supervisor_state=supervisor_state(run_id=run_id, pid=pid),
    )
    (run_dir / "run_supervisor.json").write_text(
        json.dumps({"pid": pid, "started_at": launched.isoformat()}),
        encoding="utf-8",
    )
    return run_dir


def test_a_run_whose_process_vanished_is_not_reported_active(
    fake_workspace,
) -> None:
    _write_abandoned_run(fake_workspace, "20260828_092152", age_seconds=8 * 3600)

    result = inspect_run_diagnosis("20260828_092152")

    assert result.condition == "stalled"
    assert result.recommended_next_action == "inspect_or_cancel"
    assert str(_DEAD_PID) in result.reason
    # Diagnosis only — resuming a run that no longer exists is not offered.
    assert all(a.tool != "orcho_run_resume" for a in result.next_actions)


def test_a_working_run_is_still_active(fake_workspace) -> None:
    """Recent progress is not a stall, whatever the pid probe says."""
    _write_abandoned_run(fake_workspace, "20260828_fresh", age_seconds=1.0)

    assert inspect_run_diagnosis("20260828_fresh").condition == "active"


def test_a_run_that_recorded_its_end_is_not_a_stall(fake_workspace) -> None:
    _write_abandoned_run(
        fake_workspace, "20260828_ended", age_seconds=8 * 3600, terminal_event=True,
    )

    assert inspect_run_diagnosis("20260828_ended").condition != "stalled"


@pytest.mark.parametrize("pid", [None, 0, -1])
def test_no_usable_pid_is_never_a_stall(fake_workspace, pid: object) -> None:
    """Unknown must never render as dead."""
    run_dir = _write_abandoned_run(
        fake_workspace, "20260828_nopid", age_seconds=8 * 3600,
    )
    (run_dir / "run_supervisor.json").write_text(
        json.dumps({
            "pid": pid,
            "started_at": (datetime.now(UTC) - timedelta(hours=8)).isoformat(),
        }),
        encoding="utf-8",
    )

    assert inspect_run_diagnosis("20260828_nopid").condition == "active"
