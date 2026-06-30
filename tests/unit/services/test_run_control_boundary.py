"""Unit tests for ``run_control_boundary.project_run_control`` (T1).

The durable controllability classifier reads exactly one signal —
``<run_dir>/mcp_supervisor.json`` with a resolvable ``project_dir`` (or its
``cwd`` fallback) — and never the volatile ``supervisor._runs`` registry. These
tests pin the four durable cases:

(a) MCP-started run (meta.json + mcp_supervisor.json) → mcp_controllable,
(b) CLI/foreign run dir (meta.json only) → inspect_only,
(c) supervisor state present but missing project_dir/cwd → inspect_only,
(d) genuinely missing run_id → RunNotFoundError.
"""
from __future__ import annotations

import pytest

from orcho_mcp.errors import RunNotFoundError
from orcho_mcp.services.run_control_boundary import project_run_control
from tests.fixtures.mcp_workspace import meta, supervisor_state, write_run


def test_mcp_started_run_is_controllable(fake_workspace):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="halted", project="/p/x", task="t"),
        supervisor_state=supervisor_state(
            run_id="20260101_000001", status="interrupted",
            project_dir="/p/x",
        ),
    )

    proj = project_run_control("20260101_000001")

    assert proj.control == "mcp_controllable"
    assert proj.has_supervisor_state is True
    assert proj.project_dir == "/p/x"
    assert "project_dir=/p/x" in proj.reason


def test_foreign_run_dir_is_inspect_only(fake_workspace):
    # CLI/foreign run: only meta.json on disk, no mcp_supervisor.json.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="halted", project="/p/x", task="t"),
    )

    proj = project_run_control("20260101_000001")

    assert proj.control == "inspect_only"
    assert proj.has_supervisor_state is False
    assert proj.project_dir is None
    assert "no mcp_supervisor.json" in proj.reason


def test_supervisor_state_without_project_dir_is_inspect_only(fake_workspace):
    # State file is readable but carries neither project_dir nor cwd — the
    # durable facts supervisor.resume needs are absent, so inspect_only.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="halted", project="/p/x", task="t"),
        supervisor_state={
            "run_id": "20260101_000001", "pid": 1, "status": "interrupted",
        },
    )

    proj = project_run_control("20260101_000001")

    assert proj.control == "inspect_only"
    assert proj.has_supervisor_state is True
    assert proj.project_dir is None
    assert "no resolvable project_dir" in proj.reason


def test_supervisor_state_cwd_fallback_is_controllable(fake_workspace):
    # ``cwd`` is the documented fallback supervisor.resume reads when
    # ``project_dir`` is absent; it makes the run controllable too.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="halted", project="/p/x", task="t"),
        supervisor_state={
            "run_id": "20260101_000001", "pid": 1, "status": "interrupted",
            "cwd": "/p/y",
        },
    )

    proj = project_run_control("20260101_000001")

    assert proj.control == "mcp_controllable"
    assert proj.project_dir == "/p/y"


def test_missing_run_raises(fake_workspace):
    with pytest.raises(RunNotFoundError):
        project_run_control("20260101_999999")
