"""``delivery_inconsistent`` across the MCP surfaces (ADR 0191).

Models the dogfood run ``20260907_100016_02dcb1``: the engine created a
delivery commit and crashed before its audit; ``meta.json`` stayed on
``running`` with no delivery block while the supervisor reaped the process
as ``failed``. Core owns the probe (delivery ledger + read-only git); MCP
must mirror it on every surface — diagnosis, resume pre-flight, and the live
terminal card — and never advertise a resume.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pipeline.engine.delivery_ledger import default_delivery_subject

from orcho_mcp.run_control.lifecycle import resume_run
from orcho_mcp.services.run_projection import project_run_diagnosis
from orcho_mcp.supervisor import RunHandle
from orcho_mcp.tools import orcho_run_diagnose, orcho_run_live_status
from tests.fixtures.mcp_workspace import (
    init_git_repo,
    meta,
    supervisor_state,
    write_run,
)

RUN_ID = "20260907_100016_02dcb1"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _repo(root: Path, *, delivered: bool = True) -> tuple[Path, str | None]:
    repo = root / "project"
    init_git_repo(repo)
    if not delivered:
        return repo, None
    (repo / "app.txt").write_text("run\n", encoding="utf-8")
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-q", "-m", default_delivery_subject(RUN_ID))
    return repo, _git(repo, "rev-parse", "HEAD")


def _crashed_run(workspace: Path, repo: Path, **meta_extra) -> None:
    """Stale ``running`` meta + a supervisor that reaped the process as failed."""
    write_run(
        workspace, RUN_ID,
        meta=meta(status="running", project=str(repo), task="slice A", **meta_extra),
        supervisor_state=supervisor_state(
            run_id=RUN_ID, status="failed", project_dir=str(repo),
            exit_code=1, halt_reason="abnormal_exit:1",
        ),
    )


class _SpySupervisor:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.resume_calls: list[dict] = []

    async def resume(self, run_id: str, *, profile: str | None = None):
        self.resume_calls.append({"run_id": run_id, "profile": profile})
        return RunHandle(
            run_id=run_id, pid=4321, pgid=4321, run_dir=self.tmp_path,
            project_dir="/p", command=["python"], started_at="2026-09-07T12:00:00.000Z",
        )


def test_diagnosis_reports_the_unrecorded_delivery(fake_workspace, tmp_path):
    repo, sha = _repo(tmp_path)
    _crashed_run(fake_workspace, repo)

    diag = project_run_diagnosis(RUN_ID)

    assert diag.condition == "delivery_inconsistent"
    assert diag.recommended_next_action == "reconcile_delivery"
    assert diag.status == "failed"
    assert sha[:12] in diag.reason
    assert f"orcho reconcile-delivery {RUN_ID}" in diag.reason
    # The tool surfaces the same core verdict verbatim.
    assert orcho_run_diagnose(RUN_ID).condition == "delivery_inconsistent"


@pytest.mark.asyncio
async def test_resume_is_refused_before_spawn(fake_workspace, tmp_path, monkeypatch):
    repo, _sha = _repo(tmp_path)
    _crashed_run(fake_workspace, repo)
    fake = _SpySupervisor(tmp_path)
    monkeypatch.setattr("orcho_mcp.supervisor.get_supervisor", lambda: fake)

    result = await resume_run(RUN_ID)

    assert result.resume_outcome == "delivery_inconsistent"
    assert result.run_id == RUN_ID
    assert "reconcile-delivery" in result.message
    assert not hasattr(result, "pid")
    assert result.next_actions
    assert all(na.kind == "ready_call" for na in result.next_actions)
    assert all(na.tool != "orcho_run_resume" for na in result.next_actions)
    assert fake.resume_calls == []


def test_live_terminal_card_marks_delivery_unknown_and_inconsistent(
    fake_workspace, tmp_path,
):
    repo, _sha = _repo(tmp_path)
    _crashed_run(fake_workspace, repo)

    card = orcho_run_live_status(RUN_ID)

    assert card.state_class == "terminal_halted"
    assert card.terminal is not None
    assert card.terminal.delivery_committed is None
    assert "delivery_commit_unrecorded" in card.terminal.inconsistencies
    assert card.terminal.resume_meaningful is False
    assert "reconcile-delivery" in card.next_action


def test_a_recorded_delivery_is_consistent(fake_workspace, tmp_path):
    repo, sha = _repo(tmp_path)
    _crashed_run(
        fake_workspace, repo,
        commit_delivery={"status": "committed", "commit_sha": sha},
    )

    assert project_run_diagnosis(RUN_ID).condition == "failed"
    card = orcho_run_live_status(RUN_ID)
    assert card.terminal.delivery_committed is True
    assert "delivery_commit_unrecorded" not in card.terminal.inconsistencies


def test_unknown_is_distinct_from_a_recorded_absence(fake_workspace, tmp_path):
    repo, _none = _repo(tmp_path, delivered=False)
    # A failure terminal with no delivery block: unknown, not "no delivery".
    _crashed_run(fake_workspace, repo)
    card = orcho_run_live_status(RUN_ID)
    assert card.terminal.delivery_committed is None
    assert card.terminal.inconsistencies == []
    assert project_run_diagnosis(RUN_ID).condition == "failed"

    # A clean terminal without a delivery block records an absence.
    write_run(
        fake_workspace, "20260907_000000_aaaaaa",
        meta=meta(status="done", project=str(repo), task="t"),
    )
    done = orcho_run_live_status("20260907_000000_aaaaaa")
    assert done.terminal.delivery_committed is False
