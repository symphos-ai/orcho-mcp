"""The startup recovery probe is actually wired into server startup (J1).

``RunsSupervisor.recover()`` is the only automatic path that notices a run
abandoned by a previous server process: the reaper that would have recorded
the exit is an asyncio task inside that process, so when the process dies
the run keeps saying ``running`` forever. The probe was implemented, and
documented as running "on restart", but nothing outside the test suite ever
called it — four runs on one operator's machine were reported as live for
8-23 hours after their processes were gone.

These tests pin the wiring itself, plus the rule that a probe failure must
never stop the server from starting.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orcho_mcp import server


def _write_abandoned_run(fake_workspace, run_id: str) -> Path:
    run_dir = fake_workspace / "runspace" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "mcp_supervisor.json").write_text(json.dumps({
        "run_id": run_id,
        "pid": 99999999,  # very likely dead
        "pgid": 99999999,
        "command": ["x"],
        "cwd": "/p",
        "project_dir": "/p",
        "started_at": "2026-08-28T09:21:52.000Z",
        "status": "running",
    }))
    return run_dir


def test_startup_probe_orphans_a_run_whose_process_is_gone(
    fake_workspace, capsys,
) -> None:
    run_dir = _write_abandoned_run(fake_workspace, "20260828_092152_bf675f")

    server._recover_abandoned_runs()

    state = json.loads((run_dir / "mcp_supervisor.json").read_text())
    assert state["status"] == "orphaned"
    # Announced on stderr — stdio stdout carries protocol frames only.
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "20260828_092152_bf675f" in captured.err


def test_startup_probe_is_silent_when_nothing_was_abandoned(
    fake_workspace, capsys,
) -> None:
    server._recover_abandoned_runs()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_startup_probe_failure_does_not_stop_the_server(
    fake_workspace, monkeypatch, capsys,
) -> None:
    """The worst case of a failed probe is the stale state it would clear."""
    class _Boom:
        def recover(self):
            raise RuntimeError("runs dir exploded")

    monkeypatch.setattr(
        "orcho_mcp.supervisor.get_supervisor", lambda: _Boom(),
    )

    server._recover_abandoned_runs()  # must not raise

    assert "run recovery skipped" in capsys.readouterr().err


def test_main_runs_the_probe_before_serving(monkeypatch) -> None:
    """Ordering matters: a stale run must be settled before the first call."""
    calls: list[str] = []
    monkeypatch.setattr(server, "_register_handlers", lambda: calls.append("register"))
    monkeypatch.setattr(
        server, "_recover_abandoned_runs", lambda: calls.append("recover"),
    )
    monkeypatch.setattr(
        server.anyio, "run", lambda *a, **k: calls.append("serve"),
    )

    assert server.main([]) == 0
    assert calls == ["register", "recover", "serve"]


@pytest.mark.parametrize("status", ["awaiting_phase_handoff", "done"])
def test_startup_probe_leaves_non_running_states_alone(
    fake_workspace, status: str,
) -> None:
    """A paused run's dead pid is the expected signature, not an orphan."""
    run_dir = _write_abandoned_run(fake_workspace, f"20260828_{status[:6]}")
    state_path = run_dir / "mcp_supervisor.json"
    state = json.loads(state_path.read_text())
    state["status"] = status
    state_path.write_text(json.dumps(state))

    server._recover_abandoned_runs()

    assert json.loads(state_path.read_text())["status"] == status
