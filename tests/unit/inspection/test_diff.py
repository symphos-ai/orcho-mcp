"""Unit tests for ``orcho_run_diff``.

Reads the run's captured ``diff.patch`` artifact under typed
projections (``preview``, ``stat``, ``full``) with a byte cap.
Missing artifact returns ``found=False`` rather than raising —
clean runs and runs predating the artifact are both valid.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orcho_mcp.errors import InvalidPlanError, RunNotFoundError
from orcho_mcp.tools import orcho_run_diff
from tests.fixtures.mcp_workspace import write_run

_DIFF_PATCH = (
    "diff --git a/api/payload.py b/api/payload.py\n"
    "index abc1234..def5678 100644\n"
    "--- a/api/payload.py\n"
    "+++ b/api/payload.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
    "diff --git a/api/util.py b/api/util.py\n"
    "new file mode 100644\n"
    "index 0000000..abc1234\n"
    "--- /dev/null\n"
    "+++ b/api/util.py\n"
    "@@ -0,0 +1 @@\n"
    "+helper\n"
)


def _write_run_with_diff(
    workspace: Path, run_id: str, patch: str | None = _DIFF_PATCH,
) -> Path:
    run_dir = write_run(workspace, run_id, meta={"project": "/p"})
    if patch is not None:
        (run_dir / "diff.patch").write_text(patch, encoding="utf-8")
    return run_dir


def test_run_diff_missing_artifact_returns_found_false(fake_workspace):
    _write_run_with_diff(fake_workspace, "20260519_300000", patch=None)
    r = orcho_run_diff("20260519_300000")
    assert r.run_id == "20260519_300000"
    assert r.found is False
    assert r.files == []
    assert r.content == ""
    assert r.message and "No diff artifact" in r.message


def test_run_diff_preview_mode_default(fake_workspace):
    _write_run_with_diff(fake_workspace, "20260519_300001")
    r = orcho_run_diff("20260519_300001")
    assert r.mode == "preview"
    assert r.found is True
    assert "Update(api/payload.py)" in r.content
    assert "\033[" not in r.content  # color is hard-coded False on MCP
    paths = {f.path for f in r.files}
    assert paths == {"api/payload.py", "api/util.py"}


def test_run_diff_stat_mode(fake_workspace):
    _write_run_with_diff(fake_workspace, "20260519_300002")
    r = orcho_run_diff("20260519_300002", mode="stat")
    assert r.mode == "stat"
    assert "+1 -1" in r.content
    assert "+1 -0" in r.content


def test_run_diff_full_mode_returns_raw(fake_workspace):
    _write_run_with_diff(fake_workspace, "20260519_300003")
    r = orcho_run_diff("20260519_300003", mode="full")
    assert r.mode == "full"
    assert r.content == _DIFF_PATCH


def test_run_diff_path_filter(fake_workspace):
    _write_run_with_diff(fake_workspace, "20260519_300004")
    r = orcho_run_diff("20260519_300004", mode="stat", path="api/util.py")
    assert len(r.files) == 1
    assert r.files[0].path == "api/util.py"


def test_run_diff_path_filter_with_full_emits_valid_patch(fake_workspace):
    _write_run_with_diff(fake_workspace, "20260519_300005")
    r = orcho_run_diff("20260519_300005", mode="full", path="api/util.py")
    assert "diff --git a/api/util.py" in r.content
    assert "--- /dev/null" in r.content
    assert "+++ b/api/util.py" in r.content


def test_run_diff_max_bytes_truncates(fake_workspace):
    _write_run_with_diff(fake_workspace, "20260519_300006")
    r = orcho_run_diff("20260519_300006", mode="full", max_bytes=50)
    assert r.truncated is True
    assert len(r.content.encode("utf-8")) <= 50
    assert r.max_bytes == 50


def test_run_diff_max_bytes_zero_raises(fake_workspace):
    _write_run_with_diff(fake_workspace, "20260519_300007")
    with pytest.raises(InvalidPlanError, match="max_bytes"):
        orcho_run_diff("20260519_300007", max_bytes=0)


def test_run_diff_max_bytes_above_cap_raises(fake_workspace):
    _write_run_with_diff(fake_workspace, "20260519_300008")
    with pytest.raises(InvalidPlanError, match="max_bytes"):
        orcho_run_diff("20260519_300008", max_bytes=10_000_000)


def test_run_diff_empty_path_raises(fake_workspace):
    _write_run_with_diff(fake_workspace, "20260519_300009")
    with pytest.raises(InvalidPlanError, match="path"):
        orcho_run_diff("20260519_300009", path="")


def test_run_diff_unknown_run_id_raises(fake_workspace):
    with pytest.raises(RunNotFoundError):
        orcho_run_diff("does_not_exist")


def test_run_diff_files_reflects_filtered_slice(fake_workspace):
    _write_run_with_diff(fake_workspace, "20260519_300010")
    r = orcho_run_diff("20260519_300010", mode="stat", path="api/util.py")
    assert len(r.files) == 1
    assert r.files[0].path == "api/util.py"
    assert r.files[0].added == 1
    assert r.files[0].removed == 0


# ── Per-phase diff reads ───────────────────────────────────────────────


_PHASE_PATCH = (
    "diff --git a/api/payload.py b/api/payload.py\n"
    "index abc1234..def5678 100644\n"
    "--- a/api/payload.py\n"
    "+++ b/api/payload.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+phase-only\n"
)


def _write_run_with_phase_diff(
    workspace: Path,
    run_id: str,
    phase: str,
    patch: str | None = _PHASE_PATCH,
    *,
    also_write_root_diff: str | None = None,
) -> Path:
    run_dir = write_run(workspace, run_id, meta={"project": "/p"})
    if patch is not None:
        phase_dir = run_dir / "phases" / phase
        phase_dir.mkdir(parents=True)
        (phase_dir / "diff.patch").write_text(patch, encoding="utf-8")
    if also_write_root_diff is not None:
        (run_dir / "diff.patch").write_text(
            also_write_root_diff, encoding="utf-8",
        )
    return run_dir


def test_run_diff_phase_none_regression_guard(fake_workspace):
    """``phase=None`` keeps reading the run-level cumulative diff."""
    _write_run_with_phase_diff(
        fake_workspace, "20260601_400000", "implement",
        patch=_PHASE_PATCH,
        also_write_root_diff=_DIFF_PATCH,
    )
    r = orcho_run_diff("20260601_400000", mode="full")
    assert r.found is True
    assert r.scope == "run"
    assert r.phase is None
    assert r.content == _DIFF_PATCH


def test_run_diff_phase_reads_phase_artifact(fake_workspace):
    _write_run_with_phase_diff(
        fake_workspace, "20260601_400001", "implement",
        patch=_PHASE_PATCH,
        also_write_root_diff=_DIFF_PATCH,
    )
    r = orcho_run_diff(
        "20260601_400001", mode="full", phase="implement",
    )
    assert r.found is True
    assert r.scope == "phase"
    assert r.phase == "implement"
    assert r.content == _PHASE_PATCH
    assert r.diff_path is not None
    assert r.diff_path.endswith("phases/implement/diff.patch")


def test_run_diff_phase_missing_returns_found_false_no_fallback(fake_workspace):
    """Quiet phase must NOT silently fall back to the cumulative diff."""
    _write_run_with_phase_diff(
        fake_workspace, "20260601_400002", "implement",
        patch=None,
        also_write_root_diff=_DIFF_PATCH,
    )
    r = orcho_run_diff(
        "20260601_400002", phase="repair_changes", mode="preview",
    )
    assert r.found is False
    assert r.scope == "phase"
    assert r.phase == "repair_changes"
    assert r.content == ""
    assert r.diff_path is None
    assert r.message and "repair_changes" in r.message


def test_run_diff_phase_supports_path_filter(fake_workspace):
    _write_run_with_phase_diff(
        fake_workspace, "20260601_400003", "implement",
        patch=_DIFF_PATCH,
    )
    r = orcho_run_diff(
        "20260601_400003",
        mode="stat", phase="implement", path="api/util.py",
    )
    assert r.found is True
    assert r.scope == "phase"
    assert len(r.files) == 1
    assert r.files[0].path == "api/util.py"


@pytest.mark.parametrize("bad_phase", ["", "   ", "\t"])
def test_run_diff_phase_empty_raises_invalid_plan(fake_workspace, bad_phase):
    _write_run_with_phase_diff(
        fake_workspace, "20260601_400004", "implement",
    )
    with pytest.raises(InvalidPlanError, match="phase must be non-empty"):
        orcho_run_diff("20260601_400004", phase=bad_phase)


@pytest.mark.parametrize(
    "bad_phase",
    ["..", "../etc", "phases/implement", "a\\b", "phase..name"],
)
def test_run_diff_phase_traversal_raises_invalid_plan(
    fake_workspace, bad_phase,
):
    _write_run_with_phase_diff(
        fake_workspace, "20260601_400005", "implement",
    )
    with pytest.raises(
        InvalidPlanError, match="path separators or parent refs",
    ):
        orcho_run_diff("20260601_400005", phase=bad_phase)


def test_run_diff_phase_unknown_run_id_raises(fake_workspace):
    with pytest.raises(RunNotFoundError):
        orcho_run_diff("does-not-exist", phase="implement")
