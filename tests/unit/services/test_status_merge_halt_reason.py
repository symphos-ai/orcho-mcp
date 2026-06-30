"""Unit tests for ``status_merge`` halt_reason resolution.

Covers the supervisor → meta merge for the post-SIGKILL/orphan/abnormal
exit paths where ``meta.halt_reason`` is absent because the pipeline's
in-process writers never ran.
"""
from __future__ import annotations

import json
from pathlib import Path

from orcho_mcp.services.status_merge import (
    merged_halt_reason_from_meta,
    supervisor_halt_reason,
)


def _write_supervisor(run_dir: Path, **kw) -> None:
    payload = {"run_id": run_dir.name, "pid": 1, "status": "running"}
    payload.update(kw)
    (run_dir / "mcp_supervisor.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def test_meta_halt_reason_wins_when_present(tmp_path: Path) -> None:
    _write_supervisor(tmp_path, status="failed", exit_code=-9, halt_reason="signal:SIGKILL")
    meta = {"status": "halted", "halt_reason": "phase_handoff_halt"}
    assert merged_halt_reason_from_meta(meta, tmp_path) == "phase_handoff_halt"


def test_supervisor_halt_reason_fills_in_when_meta_missing(tmp_path: Path) -> None:
    _write_supervisor(tmp_path, status="failed", exit_code=-9, halt_reason="signal:SIGKILL")
    meta = {"status": "running"}
    assert merged_halt_reason_from_meta(meta, tmp_path) == "signal:SIGKILL"


def test_returns_none_when_neither_side_has_reason(tmp_path: Path) -> None:
    _write_supervisor(tmp_path, status="done", exit_code=0)
    meta = {"status": "done"}
    assert merged_halt_reason_from_meta(meta, tmp_path) is None


def test_returns_none_when_supervisor_file_absent(tmp_path: Path) -> None:
    meta = {"status": "running"}
    assert merged_halt_reason_from_meta(meta, tmp_path) is None


def test_supervisor_halt_reason_handles_missing_file(tmp_path: Path) -> None:
    assert supervisor_halt_reason(tmp_path) is None


def test_supervisor_halt_reason_handles_malformed_json(tmp_path: Path) -> None:
    (tmp_path / "mcp_supervisor.json").write_text("{not json", encoding="utf-8")
    assert supervisor_halt_reason(tmp_path) is None


def test_supervisor_halt_reason_handles_empty_string(tmp_path: Path) -> None:
    _write_supervisor(tmp_path, status="failed", halt_reason="")
    assert supervisor_halt_reason(tmp_path) is None


def test_meta_halt_reason_empty_string_falls_through_to_supervisor(tmp_path: Path) -> None:
    _write_supervisor(tmp_path, status="failed", exit_code=137, halt_reason="abnormal_exit:137")
    meta = {"status": "failed", "halt_reason": ""}
    assert merged_halt_reason_from_meta(meta, tmp_path) == "abnormal_exit:137"
