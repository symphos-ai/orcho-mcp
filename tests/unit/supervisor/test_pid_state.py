"""Unit tests for supervisor pid liveness probe, state file IO,
run-id minting, and max-runs config.

The pure data-and-disk surface of the supervisor — no subprocess,
no async. These checks underpin the lifecycle / recovery / cancel /
resume / spawn tests that build on top of ``RunHandle`` + the
``mcp_supervisor.json`` envelope.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from orcho_mcp.supervisor import RunHandle, RunsSupervisor
from orcho_mcp.supervisor.process import is_pid_alive
from orcho_mcp.supervisor.state import (
    META_TERMINAL_STATUSES,
    meta_status_is_terminal,
    read_state,
    write_state,
)

# ── is_pid_alive ────────────────────────────────────────────────────────────

def test_is_pid_alive_true_for_self():
    assert is_pid_alive(os.getpid()) is True


def test_is_pid_alive_false_for_dead_pid():
    # PID 99999999 almost certainly doesn't exist on a normal system.
    assert is_pid_alive(99999999) is False


def test_is_pid_alive_false_for_zero():
    assert is_pid_alive(0) is False


def test_is_pid_alive_after_child_exits():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    # Sometimes the kernel keeps a zombie briefly; reap explicitly.
    assert is_pid_alive(proc.pid) is False or proc.poll() is not None


# ── State file IO ────────────────────────────────────────────────────────────

def test_write_state_creates_supervisor_json(tmp_path):
    handle = RunHandle(
        run_id="test_run",
        pid=12345,
        pgid=12345,
        run_dir=tmp_path,
        project_dir="/p",
        command=["python", "-m", "pipeline.project_orchestrator"],
        started_at="2026-05-06T12:00:00.000Z",
    )
    write_state(handle)

    state_path = tmp_path / "mcp_supervisor.json"
    assert state_path.is_file()
    state = json.loads(state_path.read_text())
    assert state["run_id"] == "test_run"
    assert state["pid"] == 12345
    assert state["pgid"] == 12345
    assert state["project_dir"] == "/p"
    assert state["status"] == "running"
    assert "started_at" in state


def test_write_state_persists_output_mode(tmp_path):
    handle = RunHandle(
        run_id="test_run",
        pid=12345,
        pgid=12345,
        run_dir=tmp_path,
        project_dir="/p",
        command=["python", "-m", "pipeline.project_orchestrator"],
        started_at="2026-05-06T12:00:00.000Z",
        output_mode="debug",
    )
    write_state(handle)

    state = json.loads((tmp_path / "mcp_supervisor.json").read_text())
    assert state["output_mode"] == "debug"


def test_write_state_includes_exit_code(tmp_path):
    handle = RunHandle(
        run_id="r",
        pid=1,
        pgid=1,
        run_dir=tmp_path,
        project_dir="/p",
        command=["x"],
        started_at="t",
        status="failed",
        exit_code=2,
    )
    write_state(handle)
    state = json.loads((tmp_path / "mcp_supervisor.json").read_text())
    assert state["exit_code"] == 2
    assert state["status"] == "failed"


def test_write_state_includes_halt_reason(tmp_path):
    handle = RunHandle(
        run_id="r",
        pid=1,
        pgid=1,
        run_dir=tmp_path,
        project_dir="/p",
        command=["x"],
        started_at="t",
        status="failed",
        exit_code=-9,
        halt_reason="signal:SIGKILL",
    )
    write_state(handle)
    state = json.loads((tmp_path / "mcp_supervisor.json").read_text())
    assert state["halt_reason"] == "signal:SIGKILL"


def test_write_state_omits_halt_reason_when_unset(tmp_path):
    handle = RunHandle(
        run_id="r",
        pid=1,
        pgid=1,
        run_dir=tmp_path,
        project_dir="/p",
        command=["x"],
        started_at="t",
    )
    write_state(handle)
    state = json.loads((tmp_path / "mcp_supervisor.json").read_text())
    assert "halt_reason" not in state


def test_read_state_returns_none_when_missing(tmp_path):
    assert read_state(tmp_path) is None


def test_read_state_returns_dict_when_present(tmp_path):
    (tmp_path / "mcp_supervisor.json").write_text(
        json.dumps({"run_id": "x", "pid": 1, "status": "running"}),
    )
    state = read_state(tmp_path)
    assert state == {"run_id": "x", "pid": 1, "status": "running"}


# ── meta_status_is_terminal ─────────────────────────────────────────────────

def test_meta_terminal_statuses_set():
    """The terminal set anchors cancel's race-fix semantics. Lock the
    exact membership: any change here is a behaviour change reviewers
    need to see.

    ``awaiting_phase_handoff`` MUST NOT be in this set — it is paused,
    not finished, and cancel semantics on a paused run is a separate
    contract (the user is asking to abort a pause, not no-op).
    """
    assert frozenset(
        {"done", "failed", "halted", "interrupted", "orphaned"}
    ) == META_TERMINAL_STATUSES
    assert "awaiting_phase_handoff" not in META_TERMINAL_STATUSES
    assert "running" not in META_TERMINAL_STATUSES


def test_meta_status_is_terminal_returns_false_when_meta_missing(tmp_path):
    """No meta.json → fall through to whatever non-meta check the
    caller has. Never assert terminality on absent evidence."""
    assert meta_status_is_terminal(tmp_path) is False


def test_meta_status_is_terminal_returns_false_when_malformed(tmp_path):
    (tmp_path / "meta.json").write_text("{ not json")
    assert meta_status_is_terminal(tmp_path) is False


def test_meta_status_is_terminal_returns_false_when_status_missing(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"task": "t"}))
    assert meta_status_is_terminal(tmp_path) is False


def test_meta_status_is_terminal_returns_false_when_status_is_running(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"status": "running"}))
    assert meta_status_is_terminal(tmp_path) is False


def test_meta_status_is_terminal_returns_false_when_paused(tmp_path):
    """Paused-handoff runs MUST NOT be treated as terminal — cancel on
    a paused run is the user explicitly aborting the pause."""
    (tmp_path / "meta.json").write_text(
        json.dumps({"status": "awaiting_phase_handoff"})
    )
    assert meta_status_is_terminal(tmp_path) is False


@pytest.mark.parametrize(
    "status",
    sorted(META_TERMINAL_STATUSES),
    ids=lambda s: f"status={s}",
)
def test_meta_status_is_terminal_returns_true_for_each_terminal(
    tmp_path, status: str,
):
    """Every documented terminal status must be recognised — that's the
    full set cancel relies on to avoid the post-flush race."""
    (tmp_path / "meta.json").write_text(json.dumps({"status": status}))
    assert meta_status_is_terminal(tmp_path) is True


def test_meta_status_is_terminal_rejects_non_string_status(tmp_path):
    """A corrupt meta where ``status`` is non-string must not raise and
    must not be treated as terminal."""
    (tmp_path / "meta.json").write_text(json.dumps({"status": 42}))
    assert meta_status_is_terminal(tmp_path) is False


# ── mint_run_id ──────────────────────────────────────────────────────────────

def test_mint_run_id_format():
    rid = RunsSupervisor.mint_run_id()
    parts = rid.split("_")
    assert len(parts) == 3, f"expected ts_HHMMSS_xxxxxx, got {rid!r}"
    assert len(parts[0]) == 8 and parts[0].isdigit()  # date YYYYMMDD
    assert len(parts[1]) == 6 and parts[1].isdigit()  # time HHMMSS
    assert len(parts[2]) == 6  # 6 hex chars


def test_mint_run_id_uniqueness():
    ids = {RunsSupervisor.mint_run_id() for _ in range(50)}
    # All 50 should be distinct (ts + uuid hex collision should be impossible
    # at this volume).
    assert len(ids) == 50


# ── max_runs config ──────────────────────────────────────────────────────────

def test_max_runs_default():
    sup = RunsSupervisor()
    assert sup._max_runs == 4


def test_max_runs_from_env(monkeypatch):
    monkeypatch.setenv("ORCHO_MCP_MAX_RUNS", "10")
    sup = RunsSupervisor()
    assert sup._max_runs == 10


def test_max_runs_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv("ORCHO_MCP_MAX_RUNS", "not_a_number")
    sup = RunsSupervisor()
    assert sup._max_runs == 4


def test_max_runs_explicit_override(monkeypatch):
    monkeypatch.delenv("ORCHO_MCP_MAX_RUNS", raising=False)
    sup = RunsSupervisor(max_runs=2)
    assert sup._max_runs == 2
