"""L4 retained-change correction launch contract through MCP and core.

The external pipeline process is deterministic here, but the control path is
real: MCP supervisor → ``followup.execute`` → core
``launch_correction_followup``.  The fake process writes the durable output a
mock correction pipeline would produce, allowing this smoke to assert the
cross-boundary launch and delivery invariants without a provider.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.mcp_workspace import init_git_repo, supervisor_state, write_run

pytestmark = pytest.mark.mcp_integration


class _CompletedProcess:
    pid = 424242

    def wait(self) -> int:
        return 0


def _event(seq: int, kind: str, phase: str | None = None) -> dict[str, object]:
    return {"seq": seq, "ts": "2026-01-01T00:00:00Z", "kind": kind, "phase": phase, "payload": {}}


@pytest.mark.asyncio
async def test_mock_correction_followup_preserves_worktree_and_supersedes_parent(
    mock_project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepted MCP followup reaches the real core seam and persists continuity."""
    from sdk.run_control import launch as core_launch

    from orcho_mcp.tools import orcho_run_diagnose, orcho_run_resume

    workspace = mock_project.parent
    retained = workspace / "retained-worktree"
    init_git_repo(retained)
    (retained / "fix.py").write_text("fixed = True\n", encoding="utf-8")
    parent_id = "rejected-parent"
    parent_dir = write_run(
        workspace,
        parent_id,
        meta={
            "status": "halted", "halt_reason": "final_acceptance_rejected",
            "project": str(mock_project), "task": "repair rejected change",
            "profile": "feature",
            "worktree": {"isolation": "worktree", "path": str(retained)},
            "phases": {"final_acceptance": {"verdict": "REJECTED", "release_blockers": [{"id": "R1", "detail": "fix"}]}},
        },
    )
    spawned: list[list[str]] = []

    def fake_spawn(cmd, *, project_dir, env, log_fd):
        spawned.append(cmd)
        child_dir = parent_dir.parent / env["ORCHO_RUN_ID"]
        child_dir.mkdir(exist_ok=True)
        child_dir.joinpath("meta.json").write_text(json.dumps({
            "status": "done", "resume_mode": "followup", "parent_run_id": parent_id,
            "parent_run_dir": str(parent_dir), "parent_status": "halted",
            "profile": "correction", "task": "repair rejected change",
            "worktree": {"isolation": "worktree", "path": str(retained), "followup_continuity": {"mode_label": "reused_parent", "blocked": False, "reason": None, "diff_source": "worktree"}},
        }), encoding="utf-8")
        child_dir.joinpath("events.jsonl").write_text(
            "\n".join(json.dumps(event) for event in [
                _event(1, "phase.start", "CORRECTION_TRIAGE"),
                _event(2, "phase.start", "IMPLEMENT"),
                _event(3, "run.end"),
            ]) + "\n", encoding="utf-8",
        )
        parent = json.loads(parent_dir.joinpath("meta.json").read_text(encoding="utf-8"))
        parent["status"] = "done"
        parent["superseded_by_followup"] = {
            "child_run_id": env["ORCHO_RUN_ID"], "child_status": "done",
            "delivery_status": "committed", "reason": "ordinary correction followup",
        }
        parent_dir.joinpath("meta.json").write_text(json.dumps(parent), encoding="utf-8")
        return _CompletedProcess()

    monkeypatch.setattr(core_launch, "_spawn_detached", fake_spawn)
    correction = await orcho_run_resume(
        parent_id, operator_intent="followup", operator_comment="apply reviewer fix",
    )

    assert correction.resume_outcome == "followup_started"
    child_dir = Path(correction.run_dir)
    child = json.loads(child_dir.joinpath("meta.json").read_text(encoding="utf-8"))
    assert child["resume_mode"] == "followup"
    assert child["parent_run_id"] == parent_id
    assert child["profile"] == "correction"
    assert child["worktree"]["path"] == str(retained)
    assert "--resume" in spawned[0] and parent_id in spawned[0]
    assert "--profile" in spawned[0] and "correction" in spawned[0]
    phases = [event["phase"] for event in map(json.loads, child_dir.joinpath("events.jsonl").read_text().splitlines()) if event["kind"] == "phase.start"]
    assert phases[0] == "CORRECTION_TRIAGE"
    assert "PLAN" not in phases and "VALIDATE_PLAN" not in phases
    parent = json.loads(parent_dir.joinpath("meta.json").read_text(encoding="utf-8"))
    assert parent["superseded_by_followup"]["child_run_id"] == correction.run_id
    assert "from_run_plan" not in orcho_run_diagnose(parent_id).model_dump_json()


@pytest.mark.asyncio
async def test_bare_terminal_resume_does_not_register_supervisor_run(mock_project: Path) -> None:
    """Negative L4: a terminal parent remains inert and creates no handle."""
    from orcho_mcp.supervisor import get_supervisor
    from orcho_mcp.tools import orcho_run_resume

    run_id = "terminal-parent"
    write_run(
        mock_project.parent, run_id,
        meta={"status": "done", "project": str(mock_project), "task": "done"},
        supervisor_state=supervisor_state(run_id=run_id, status="done", project_dir=str(mock_project)),
    )
    supervisor = get_supervisor()
    before = dict(supervisor._runs)
    result = await orcho_run_resume(run_id)

    assert result.resume_outcome == "rejected_terminal"
    assert supervisor._runs == before
