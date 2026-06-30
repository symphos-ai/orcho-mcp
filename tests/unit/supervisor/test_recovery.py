"""Unit tests for supervisor restart-recovery / orphan handling.

``RunsSupervisor.recover()`` scans the runs directory on startup and
marks abandoned in-flight runs as orphaned (status="orphaned" +
``run.orphaned`` event). Paused-handoff runs (rc=4 → status=
``awaiting_phase_handoff``) MUST stay paused even when their pid is
dead. Terminal-state runs are skipped entirely. The probe is no-op
when the workspace can't be resolved.

Uses the shared ``fake_workspace`` fixture
(``tests/fixtures/mcp_workspace.py``) so every supervisor file gets
the same temp tree layout without duplicating setup.
"""
from __future__ import annotations

import json
import os

from orcho_mcp.supervisor import RunsSupervisor


def test_recover_marks_dead_pid_as_orphaned(fake_workspace):
    runs_dir = fake_workspace / "runspace" / "runs"

    run_dir = runs_dir / "20260506_test_aa1111"
    run_dir.mkdir()
    (run_dir / "mcp_supervisor.json").write_text(json.dumps({
        "run_id":     "20260506_test_aa1111",
        "pid":        99999999,  # very likely dead
        "pgid":       99999999,
        "command":    ["x"],
        "cwd":        "/p",
        "project_dir": "/p",
        "started_at": "2026-05-06T12:00:00.000Z",
        "status":     "running",
    }))

    sup = RunsSupervisor()
    orphaned = sup.recover()

    assert "20260506_test_aa1111" in orphaned
    state = json.loads((run_dir / "mcp_supervisor.json").read_text())
    assert state["status"] == "orphaned"

    # Orphan event was appended.
    events_path = run_dir / "events.jsonl"
    assert events_path.is_file()
    lines = [line for line in events_path.read_text().splitlines() if line.strip()]
    assert any("run.orphaned" in line for line in lines)


def test_recover_leaves_live_pid_alone(fake_workspace):
    runs_dir = fake_workspace / "runspace" / "runs"

    run_dir = runs_dir / "20260506_test_bb2222"
    run_dir.mkdir()
    (run_dir / "mcp_supervisor.json").write_text(json.dumps({
        "run_id":     "20260506_test_bb2222",
        "pid":        os.getpid(),  # this very process — definitely alive
        "pgid":       os.getpid(),
        "command":    ["x"],
        "cwd":        "/p",
        "project_dir": "/p",
        "started_at": "2026-05-06T12:00:00.000Z",
        "status":     "running",
    }))

    sup = RunsSupervisor()
    orphaned = sup.recover()

    assert "20260506_test_bb2222" not in orphaned
    state = json.loads((run_dir / "mcp_supervisor.json").read_text())
    assert state["status"] == "running"


def test_recover_does_not_orphan_awaiting_phase_handoff(fake_workspace):
    """Paused-handoff runs MUST stay paused, not be orphaned, even with dead pid.

    The pipeline exits with rc=4 when a phase's declared handoff policy
    fires — the dead pid is the *expected* signature of a paused run,
    not an orphan condition. Orphaning would force a client to
    re-spawn instead of resuming the existing checkpoint via
    ``orcho_phase_handoff_decide(..., action="continue")`` +
    ``orcho_run_resume``.
    """
    runs_dir = fake_workspace / "runspace" / "runs"
    run_dir = runs_dir / "20260506_test_qa_paused"
    run_dir.mkdir()
    (run_dir / "mcp_supervisor.json").write_text(json.dumps({
        "run_id":     "20260506_test_qa_paused",
        "pid":        99999999,  # dead pid — pipeline already exited
        "pgid":       99999999,
        "command":    ["x"],
        "cwd":        "/p",
        "project_dir": "/p",
        "started_at": "2026-05-06T12:00:00.000Z",
        "status":     "awaiting_phase_handoff",
        "exit_code":  4,
    }))

    sup = RunsSupervisor()
    orphaned = sup.recover()

    assert "20260506_test_qa_paused" not in orphaned
    state = json.loads((run_dir / "mcp_supervisor.json").read_text())
    assert state["status"] == "awaiting_phase_handoff", (
        "paused QA run was incorrectly orphaned by recover()"
    )


def test_recover_skips_runs_in_terminal_state(fake_workspace):
    runs_dir = fake_workspace / "runspace" / "runs"

    run_dir = runs_dir / "20260506_test_cc3333"
    run_dir.mkdir()
    (run_dir / "mcp_supervisor.json").write_text(json.dumps({
        "run_id":  "20260506_test_cc3333",
        "pid":     99999999,
        "pgid":    99999999,
        "command": ["x"],
        "cwd":     "/p",
        "project_dir": "/p",
        "started_at": "t",
        "status":  "done",  # already terminal — not a candidate for orphaning
    }))

    sup = RunsSupervisor()
    orphaned = sup.recover()
    assert orphaned == []


def test_recover_handles_missing_workspace(monkeypatch):
    monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
    # runspace_dir() prefers $ORCHO_RUNSPACE over $ORCHO_WORKSPACE/runspace,
    # so a runner env that sets it would resolve a real shared runspace and
    # surface unrelated orphan candidates. Clear it too, mirroring the
    # fake_workspace fixture, so "missing workspace" actually holds.
    monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
    monkeypatch.chdir("/")
    sup = RunsSupervisor()
    # Doesn't crash — just returns empty.
    assert sup.recover() == []
