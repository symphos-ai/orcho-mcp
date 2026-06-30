"""Verification-environment receipt slice on ``orcho_run_evidence`` (T6).

A developer-side phase records a durable verification-environment receipt
under ``<run_dir>/verification_receipts/<phase>_round<N>.json`` describing
where it ran its work and checks. The SDK exposes only a thin summary, so
the ``verification_receipts`` evidence slice reads the durable JSON
artifacts directly. These tests pin that the slice surfaces interpreter,
cwd, import checks, command list + exit codes, the clean-tree note, and
the artifact path — and the ``all_passed`` rollup.
"""
from __future__ import annotations

import json
from pathlib import Path

from orcho_mcp.tools import orcho_run_evidence
from tests.fixtures.mcp_workspace import meta, write_run


def _receipt(
    *, phase: str, round_n: int = 1, passed: bool = True,
    expected: str = "/co/pipeline/__init__.py",
    actual: str = "/co/pipeline/__init__.py",
    exit_code: int = 0,
) -> dict:
    return {
        "phase": phase,
        "round": round_n,
        "kind": "verification_environment",
        "cwd": "/co",
        "python": "3.12.4 (/co/.venv/bin/python)",
        "checks": [{
            "name": "pipeline_import",
            "expected": expected,
            "actual": actual,
            "passed": passed,
        }],
        "commands": [{
            "argv": ["/co/.venv/bin/python", "-c", "import pipeline"],
            "exit_code": exit_code,
        }],
        "temp_env_outside_checkout": True,
    }


def _write_receipts(run_dir: Path, receipts: dict[str, dict]) -> None:
    rdir = run_dir / "verification_receipts"
    rdir.mkdir(parents=True)
    for filename, data in receipts.items():
        (rdir / filename).write_text(json.dumps(data), encoding="utf-8")


def test_verification_receipts_slice_surfaces_full_detail(fake_workspace):
    run_dir = write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    _write_receipts(run_dir, {
        "implement_round1.json": _receipt(phase="implement"),
        "repair_changes_round1.json": _receipt(phase="repair_changes"),
    })

    r = orcho_run_evidence("20260101_000001", slice="verification_receipts")

    assert r.slice == "verification_receipts"
    assert r.verification_receipts is not None
    assert len(r.verification_receipts) == 2
    # Sorted by (phase, round): implement before repair_changes.
    first = r.verification_receipts[0]
    assert first.phase == "implement"
    assert first.round == 1
    assert first.kind == "verification_environment"
    assert first.python == "3.12.4 (/co/.venv/bin/python)"
    assert first.cwd == "/co"
    assert first.temp_env_outside_checkout is True
    assert first.all_passed is True
    # Import check detail.
    assert len(first.checks) == 1
    chk = first.checks[0]
    assert chk.name == "pipeline_import"
    assert chk.expected == "/co/pipeline/__init__.py"
    assert chk.actual == "/co/pipeline/__init__.py"
    assert chk.passed is True
    # Command list + exit codes.
    assert len(first.commands) == 1
    cmd = first.commands[0]
    assert cmd.argv == ["/co/.venv/bin/python", "-c", "import pipeline"]
    assert cmd.exit_code == 0
    # Artifact path under the run dir.
    assert first.artifact_path is not None
    assert first.artifact_path.endswith("implement_round1.json")


def test_failed_import_check_rolls_up_to_not_all_passed(fake_workspace):
    run_dir = write_run(
        fake_workspace, "20260101_000002",
        meta=meta(status="failed", project="/p/x", task="t"),
    )
    _write_receipts(run_dir, {
        "implement_round1.json": _receipt(
            phase="implement", passed=False,
            expected="/co/pipeline/__init__.py",
            actual="/stable/pipeline/__init__.py",
            exit_code=0,
        ),
    })

    r = orcho_run_evidence("20260101_000002", slice="verification_receipts")

    rec = r.verification_receipts[0]
    assert rec.all_passed is False
    assert rec.checks[0].passed is False
    # The mismatch is visible: actual differs from expected.
    assert rec.checks[0].actual != rec.checks[0].expected


def test_all_slice_includes_verification_receipts(fake_workspace):
    run_dir = write_run(
        fake_workspace, "20260101_000003",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    _write_receipts(run_dir, {
        "implement_round1.json": _receipt(phase="implement"),
    })

    r = orcho_run_evidence("20260101_000003", slice="all")
    assert r.verification_receipts is not None
    assert len(r.verification_receipts) == 1


def test_no_receipts_dir_yields_empty_list(fake_workspace):
    write_run(
        fake_workspace, "20260101_000004",
        meta=meta(status="done", project="/p/x", task="t"),
    )

    r = orcho_run_evidence("20260101_000004", slice="verification_receipts")
    assert r.verification_receipts == []


def test_malformed_receipt_is_skipped(fake_workspace):
    run_dir = write_run(
        fake_workspace, "20260101_000005",
        meta=meta(status="done", project="/p/x", task="t"),
    )
    rdir = run_dir / "verification_receipts"
    rdir.mkdir(parents=True)
    (rdir / "good_round1.json").write_text(
        json.dumps(_receipt(phase="good")), encoding="utf-8",
    )
    (rdir / "broken_round1.json").write_text("{not json", encoding="utf-8")

    r = orcho_run_evidence("20260101_000005", slice="verification_receipts")
    # The broken file is skipped; the good one survives.
    assert len(r.verification_receipts) == 1
    assert r.verification_receipts[0].phase == "good"
