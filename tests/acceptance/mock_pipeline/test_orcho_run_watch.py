"""Acceptance tests for ``orcho_run_watch``.

End-to-end through a real ``python -m orcho_mcp`` subprocess plus a real
mock-mode pipeline run. The load-bearing assertion is **progressToken
capture on a real session** — proves that:

  1. ``Context.report_progress`` actually fires
     ``notifications/progress`` over the wire when the request carries a
     ``progressToken``;
  2. progress values are monotonically non-decreasing;
  3. the watch returns a populated ``RunWatchResult`` with a sensible
     ``trigger.kind`` (handoff or terminal) once the run reaches a stable
     state.

The mock pipeline is fully hermetic (every phase-agent slot is replaced
with an inline stub via ``make_mock_phase_config``), so this test runs
without any provider CLI installed.

Marked ``mcp_integration``; opt in with ``pytest -m mcp_integration``.

The ``read_timeout_seconds`` argument on ``call_tool`` is set explicitly
because ``orcho_run_watch`` intentionally holds the request open for up
to ``timeout_s``. The client read timeout must exceed that by a safety
margin so future SDK default changes do not silently make the test flaky.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import timedelta

import pytest

pytest.importorskip("mcp.client.stdio")

pytestmark = pytest.mark.mcp_integration


@pytest.fixture
def mock_project(tmp_path, monkeypatch):
    """Minimal orcho-trackable project + workspace. Same shape as the
    fixture in ``test_orcho_run_mock.py``; duplicated here to keep the L4
    file standalone and so it can be moved/promoted later without
    cross-file fixture wiring."""
    ws = tmp_path / "ws"
    project = ws / "demo_project"
    runs_dir = ws / "runspace" / "runs"
    project.mkdir(parents=True)
    runs_dir.mkdir(parents=True)
    (project / "README.md").write_text("# Demo project\n", encoding="utf-8")
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    return ws


@pytest.mark.anyio
async def test_orcho_run_watch_progress_token_capture(mock_project):
    """Real MCP session with ``progress_callback`` receives ≥ 1 ordered,
    monotonic ``notifications/progress`` event from ``orcho_run_watch``.

    Algorithm:
      1. Spawn ``python -m orcho_mcp`` over stdio with ``ORCHO_WORKSPACE``
         pointing at the mock fixture.
      2. Call ``orcho_run_start`` with ``mock=True`` to launch a real
         pipeline subprocess.
      3. Briefly wait so the pipeline has time to emit events. (Mock runs
         finish in under a second on warm caches.)
      4. Call ``orcho_run_watch`` with ``until=handoff_or_terminal``,
         a generous ``timeout_s``, and an explicit ``progress_callback``
         that records every notification.
      5. Assert progress was captured, values are monotonic, the result
         shape parses, and ``trigger.kind`` lands on handoff or terminal.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {**os.environ, "ORCHO_WORKSPACE": str(mock_project)}
    project_dir = str(mock_project / "demo_project")

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "orcho_mcp"],
        env=env,
    )

    progress_events: list[tuple[float, float | None, str | None]] = []

    async def record_progress(
        progress: float,
        total: float | None,
        message: str | None,
    ) -> None:
        progress_events.append((progress, total, message))

    async with stdio_client(params) as (read, write), ClientSession(
        read, write,
    ) as session:
        await session.initialize()

        # 1) Start a real mock run.
        start_res = await session.call_tool(
            "orcho_run_start",
            {
                "task": "watch progressToken integration",
                "project_dir": project_dir,
                "mock": True,
                "max_rounds": 1,
            },
        )
        start_payload = start_res.structuredContent
        assert start_payload is not None
        run_id = start_payload["run_id"]
        assert run_id

        # 2) Brief settle: let the mock pipeline emit at least one event
        # so the watch has something to report progress on. The mock
        # pipeline is sub-second; 0.5 s is enough to land run.start.
        time.sleep(0.5)

        # 3) Watch the run with progressToken capture.
        result = await session.call_tool(
            "orcho_run_watch",
            {
                "run_id": run_id,
                "since_seq": 0,
                "until": "handoff_or_terminal",
                "timeout_s": 30,
            },
            read_timeout_seconds=timedelta(seconds=35),
            progress_callback=record_progress,
        )

    # 4) Assertions.
    assert len(progress_events) >= 1, (
        f"expected at least one progressToken notification, got "
        f"{len(progress_events)} (events: {progress_events})"
    )
    # Monotonic non-decreasing progress.
    seqs = [p for p, _, _ in progress_events]
    assert seqs == sorted(seqs), (
        f"progress values must be monotonically non-decreasing, got {seqs}"
    )

    payload = result.structuredContent
    assert payload is not None
    assert payload["run_id"] == run_id
    assert payload["triggered"] is True
    assert payload["trigger"]["kind"] in {"handoff", "terminal"}, (
        f"expected handoff or terminal trigger after a mock run settles, "
        f"got {payload['trigger']['kind']!r}"
    )
    assert payload["summary"] is not None
    # Reconnect cursor: summary.next_seq is at least as advanced as the
    # last progress value (progress only ever lags or matches it).
    assert payload["summary"]["next_seq"] >= seqs[-1], (
        f"summary.next_seq={payload['summary']['next_seq']} < "
        f"last progress {seqs[-1]}"
    )
    # If the run paused for a decision, the hint must be populated.
    if payload["trigger"]["kind"] == "handoff":
        assert payload["handoff"] is not None
        assert payload["handoff"]["kind"] == "requires_user_decision"


@pytest.fixture
def anyio_backend():
    return "asyncio"
