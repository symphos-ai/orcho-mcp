"""L1 unit tests for ``orcho_mcp.workspace_state``.

Pure-Python tests against a ``tmp_path`` workspace — no MCP server, no
subprocess, no fixtures from ``conftest.py``. The state module is a thin
file-IO layer; these tests pin its load-bearing invariants:

- Cold start returns a valid empty envelope.
- Writes are atomic + create the file on demand.
- ``last_seq`` is monotonic per run (cursor never moves backwards).
- Same-or-newer seq refreshes status/phase/timestamp.
- Corrupt JSON is recovered to an empty envelope; the next write fixes
  the file.
- The shape contains no raw event payloads, prompts, findings, env, or
  secrets — guards the "no PII trough" rule mechanically.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orcho_mcp.workspace_state import (
    read_workspace_state,
    state_path,
    update_run_state,
)


def test_read_missing_state_returns_empty(tmp_path: Path):
    """Cold start: no file, no parent dir — must still produce a valid
    empty envelope so callers never have to special-case missing state."""
    ws = tmp_path / "ws"
    ws.mkdir()
    state = read_workspace_state(ws)
    assert state["version"] == 1
    assert state["workspace_dir"] == str(ws)
    assert state["runs"] == {}
    assert state["server_started_at"]
    assert state["updated_at"]


def test_update_run_state_creates_file_atomically(tmp_path: Path):
    """First update writes the file at ``<ws>/mcp/state.json`` and the
    on-disk contents match what the function returned."""
    ws = tmp_path / "ws"
    ws.mkdir()
    state = update_run_state(
        ws, run_id="20260101_000001",
        last_seq=42, last_status="running", last_phase="implement",
    )
    sp = state_path(ws)
    assert sp.is_file()
    on_disk = json.loads(sp.read_text(encoding="utf-8"))
    assert on_disk == state
    record = state["runs"]["20260101_000001"]
    assert record["last_seq"] == 42
    assert record["last_status"] == "running"
    assert record["last_phase"] == "implement"


def test_update_run_state_is_monotonic(tmp_path: Path):
    """Cursor must never move backwards. Writing a lower seq keeps the
    existing higher seq but still refreshes status/phase/timestamp
    (status may advance even when the event stream pauses)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    update_run_state(
        ws, run_id="r1",
        last_seq=10, last_status="running", last_phase="implement",
    )
    state = update_run_state(
        ws, run_id="r1",
        last_seq=5, last_status="awaiting_phase_handoff",
        last_phase="validate_plan",
    )
    record = state["runs"]["r1"]
    assert record["last_seq"] == 10  # cursor pinned
    # Status / phase still refresh — they describe wall-clock state,
    # not stream position.
    assert record["last_status"] == "awaiting_phase_handoff"
    assert record["last_phase"] == "validate_plan"


def test_update_run_state_allows_same_or_newer_seq_status_refresh(
    tmp_path: Path,
):
    """Same seq with new status replaces the record wholesale; the new
    status survives. This is the steady-state polling case where the
    cursor hasn't advanced but the run has paused for a handoff."""
    ws = tmp_path / "ws"
    ws.mkdir()
    update_run_state(
        ws, run_id="r1",
        last_seq=10, last_status="running", last_phase="implement",
    )
    state = update_run_state(
        ws, run_id="r1",
        last_seq=10, last_status="awaiting_phase_handoff",
        last_phase="validate_plan",
    )
    record = state["runs"]["r1"]
    assert record["last_seq"] == 10
    assert record["last_status"] == "awaiting_phase_handoff"
    assert record["last_phase"] == "validate_plan"


def test_corrupt_state_is_replaced(tmp_path: Path):
    """A partial / bogus JSON file must not break reads, and the next
    write must replace it with a valid envelope."""
    ws = tmp_path / "ws"
    ws.mkdir()
    sp = state_path(ws)
    sp.parent.mkdir(parents=True)
    sp.write_text("not valid json {{[[", encoding="utf-8")

    state = read_workspace_state(ws)
    assert state["version"] == 1
    assert state["runs"] == {}

    updated = update_run_state(
        ws, run_id="r1", last_seq=1,
        last_status="running", last_phase="plan",
    )
    assert updated["runs"]["r1"]["last_seq"] == 1
    # File now parses cleanly.
    on_disk = json.loads(sp.read_text(encoding="utf-8"))
    assert on_disk["runs"]["r1"]["last_seq"] == 1


def test_state_contains_no_raw_payload_fields(tmp_path: Path):
    """The wire shape is the contract — guard against accidentally
    growing the file into a PII / log trough.

    Any future change that adds an ``events`` / ``payload`` /
    ``findings`` / ``prompt`` / ``env`` / ``secrets`` key (top-level
    or per-run) breaks this test, which is the intent.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    state = update_run_state(
        ws, run_id="r1",
        last_seq=1, last_status="running", last_phase="plan",
    )

    banned = {
        "events", "payload", "findings", "prompt",
        "env", "secrets", "credentials",
    }

    top_level = set(state.keys())
    assert top_level.isdisjoint(banned), (
        f"unexpected top-level keys: {top_level & banned}"
    )

    for run_id, record in state["runs"].items():
        assert isinstance(record, dict)
        per_run = set(record.keys())
        assert per_run.isdisjoint(banned), (
            f"run {run_id!r} carries banned keys: {per_run & banned}"
        )
        # And the per-run shape is the strict six-field record.
        assert per_run == {
            "run_id", "last_seq", "last_status",
            "last_phase", "last_summary_at",
        }


def test_normalise_overrides_stale_workspace_dir(tmp_path: Path):
    """Regression: a state file copied / moved from another workspace
    must report the *current* resolver path, not the embedded one.

    Otherwise reconnect UX shows a path that no longer exists, and
    debugging "why did this run move?" gets harder than it needs to.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    sp = state_path(ws)
    sp.parent.mkdir(parents=True)
    # Hand-craft a state file claiming a wholly different workspace path,
    # mimicking a copy-pasted file or a moved workspace.
    bogus = {
        "version": 1,
        "workspace_dir": "/elsewhere/old/workspace",
        "server_started_at": "2020-01-01T00:00:00Z",
        "updated_at": "2020-01-01T00:00:00Z",
        "runs": {
            "r1": {
                "run_id": "r1",
                "last_seq": 7,
                "last_status": "running",
                "last_phase": "implement",
                "last_summary_at": "2020-01-01T00:00:00Z",
            },
        },
    }
    sp.write_text(json.dumps(bogus), encoding="utf-8")

    state = read_workspace_state(ws)
    # workspace_dir snaps back to the resolver-provided path.
    assert state["workspace_dir"] == str(ws)
    # Run record itself is preserved — only the envelope path changes.
    assert state["runs"]["r1"]["last_seq"] == 7


def test_update_run_state_rejects_bogus_inputs(tmp_path: Path):
    """Programmer-error inputs (empty run_id, negative seq) raise loudly
    so a bug in the wiring surfaces in CI, not silently as a missing
    state record."""
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(ValueError):
        update_run_state(
            ws, run_id="", last_seq=1,
            last_status="running", last_phase=None,
        )
    with pytest.raises(ValueError):
        update_run_state(
            ws, run_id="r1", last_seq=-1,
            last_status="running", last_phase=None,
        )
