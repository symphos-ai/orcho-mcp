"""Acceptance tests for live subtask_dag progress over MCP.

Wire proof behind the ``orcho_run_watch(until="subtask")`` feature: a real
``--mock`` subtask_dag run emits ``subtask.start`` events whose payload
carries the ``index`` / ``total`` / ``goal`` progress coordinate, and those
reach an MCP client through ``orcho_run_events_tail``. The trigger logic and
``current_subtask`` projection are pinned deterministically at L1
(``tests/unit/observe``); this layer proves the real subprocess pipeline
actually produces the enriched events.

Uses the shared git-backed ``mock_project`` fixture from
``tests/acceptance/conftest.py`` (runs reach ``done``).

Marked ``mcp_integration``; opt in with ``pytest -m mcp_integration``.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.mcp_integration


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _wait_status(run_id: str, expected: set[str], timeout_s: float = 90.0):
    from orcho_mcp.tools import orcho_run_status
    deadline = time.monotonic() + timeout_s
    last: str | None = None
    while time.monotonic() < deadline:
        last = (orcho_run_status(run_id).meta or {}).get("status")
        if last in expected:
            return last
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"run {run_id} did not reach {expected!r} in {timeout_s}s (last={last!r})"
    )


def _all_events(run_id: str):
    from orcho_mcp.tools import orcho_run_events_tail
    events = []
    since = 0
    for _ in range(200):
        res = orcho_run_events_tail(run_id, since_seq=since, limit=200)
        events.extend(res.events)
        if res.eof or not res.events:
            break
        since = res.next_seq
    return events


@pytest.mark.anyio
async def test_subtask_start_events_carry_progress_coordinates(
    mock_project: Path,
) -> None:
    """subtask.start events expose index/total/goal end-to-end."""
    from orcho_mcp.tools import orcho_run_start

    started = await orcho_run_start(
        task="subtask progress over wire",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    await _wait_status(started.run_id, {"done"})

    starts = [e for e in _all_events(started.run_id) if e.kind == "subtask.start"]
    assert starts, "expected subtask.start events from the subtask_dag implement phase"
    for e in starts:
        assert "index" in e.payload
        assert "total" in e.payload
        assert e.payload.get("goal")
    # One DAG → one total; indexes are a clean 1..N.
    totals = {e.payload["total"] for e in starts}
    assert len(totals) == 1
    indexes = sorted(e.payload["index"] for e in starts)
    assert indexes == list(range(1, len(indexes) + 1))


@pytest.mark.anyio
async def test_watch_until_subtask_terminal_on_finished_run(
    mock_project: Path,
) -> None:
    """until="subtask" against a finished run returns a terminal trigger (the
    override) — never hangs — and reports current_subtask=None once implement
    has ended."""
    from orcho_mcp.tools import orcho_run_start, orcho_run_watch

    started = await orcho_run_start(
        task="subtask watch terminal",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    await _wait_status(started.run_id, {"done"})

    result = await orcho_run_watch(
        started.run_id, since_seq=0, until="subtask", timeout_s=5,
    )
    assert result.trigger.kind == "terminal"
    assert result.summary is not None
    assert result.summary.current_subtask is None
