"""Typed ``resume_outcome`` pre-flight guard on ``orcho_run_resume`` (GC-2/T2).

Before spawning, ``resume_run`` classifies the run via the shared
``project_run_diagnosis``. A terminal run and a parent superseded by a live
follow-up child are refused *before* the supervisor is touched — the
responses carry no spawn fields and a spy on the fake supervisor proves
``resume`` was never called. A genuinely resumable run still spawns and
returns a success-shaped :class:`RunResumeResult` with a real ``pid``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orcho_mcp.errors import InspectOnlyControlError
from orcho_mcp.run_control.lifecycle import resume_run
from orcho_mcp.schemas import (
    InspectOnlyControlResult,
    ResumeBlockedResult,
    RunResumeResult,
)
from orcho_mcp.supervisor import RunHandle
from tests.fixtures.mcp_workspace import meta, supervisor_state, write_run

# Durable MCP-controllability marker: a readable ``mcp_supervisor.json`` with a
# resolvable ``project_dir`` is what makes a run ``mcp_controllable`` (the
# applied/blocked resume paths below). Without it the run is foreign /
# CLI-started and the control guard refuses resume with InspectOnlyControlResult
# (the dedicated foreign-run tests at the bottom of this file). The state status
# stays trivial (``running``) so it never overrides the non-trivial
# ``meta.status`` the diagnosis branches on.


def _controllable(run_id: str):
    return supervisor_state(run_id=run_id, project_dir="/p/x")


class _SpySupervisor:
    """Fake supervisor recording whether ``resume`` was ever invoked."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.resume_calls: list[dict] = []

    async def resume(self, run_id: str, *, profile: str | None = None):
        self.resume_calls.append({"run_id": run_id, "profile": profile})
        return RunHandle(
            run_id=run_id,
            pid=4321,
            pgid=4321,
            run_dir=self.tmp_path,
            project_dir="/p",
            command=["python", "-m", "pipeline.project_orchestrator", "--resume"],
            started_at="2026-06-19T12:00:00.000Z",
        )


def _patch_supervisor(monkeypatch, fake) -> None:
    monkeypatch.setattr("orcho_mcp.supervisor.get_supervisor", lambda: fake)


def _followup_child(parent_run_id: str, *, status: str = "running", **extra):
    return meta(
        status=status, project="/p/x", task="follow-up",
        resume_mode="followup", parent_run_id=parent_run_id, **extra,
    )


@pytest.mark.asyncio
async def test_terminal_run_rejected_without_spawn(
    fake_workspace, tmp_path, monkeypatch,
):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
        supervisor_state=_controllable("20260101_000001"),
    )
    fake = _SpySupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    result = await resume_run("20260101_000001")

    assert isinstance(result, ResumeBlockedResult)
    assert result.resume_outcome == "rejected_terminal"
    assert result.run_id == "20260101_000001"
    assert result.recommended_run_id is None
    # No spawn fields exist on this shape — model carries no pid.
    assert not hasattr(result, "pid")
    # Inspection-only follow-ups, every record a ready_call; never a resume.
    assert result.next_actions
    assert all(na.kind == "ready_call" for na in result.next_actions)
    assert {na.tool for na in result.next_actions} == {
        "orcho_run_status", "orcho_run_evidence",
    }
    assert all(na.tool != "orcho_run_resume" for na in result.next_actions)
    # The supervisor was never asked to resume a terminal run.
    assert fake.resume_calls == []


@pytest.mark.asyncio
async def test_supervisor_terminal_stale_meta_rejected_without_spawn(
    fake_workspace, tmp_path, monkeypatch,
):
    # The supervisor reaped the run terminal ('done') while ``meta.json`` is
    # still stale on 'running'. The pre-flight guard must treat the MERGED
    # status as terminal and refuse resume — otherwise a no-op resume would
    # spawn. Spy confirms supervisor.resume is never called.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="running", project="/p/x", task="t"),
        supervisor_state=supervisor_state(
            run_id="20260101_000001", status="done",
        ),
    )
    fake = _SpySupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    result = await resume_run("20260101_000001")

    assert isinstance(result, ResumeBlockedResult)
    assert result.resume_outcome == "rejected_terminal"
    assert not hasattr(result, "pid")
    assert all(na.tool != "orcho_run_resume" for na in result.next_actions)
    # The supervisor was never asked to resume the stale-meta terminal run.
    assert fake.resume_calls == []


@pytest.mark.asyncio
async def test_superseded_by_child_recommends_child_without_spawn(
    fake_workspace, tmp_path, monkeypatch,
):
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(status="done", project="/p/x", task="t"),
        supervisor_state=_controllable("20260101_000001"),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=_followup_child("20260101_000001", status="running"),
    )
    fake = _SpySupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    result = await resume_run("20260101_000001")

    assert isinstance(result, ResumeBlockedResult)
    assert result.resume_outcome == "superseded_by_child"
    assert result.recommended_run_id == "20260101_000002"
    assert len(result.next_actions) == 1
    na = result.next_actions[0]
    assert na.kind == "ready_call"
    assert na.requires_operator_input is False
    assert na.tool == "orcho_run_resume"
    assert na.args == {"run_id": "20260101_000002"}
    # The parent's resume was intercepted — no spawn.
    assert fake.resume_calls == []


_RETAINED_WORKTREE = {"isolation": "worktree", "path": "/tmp/wt/source"}


@pytest.mark.asyncio
async def test_recover_via_source_run_points_to_source_without_spawn(
    fake_workspace, tmp_path, monkeypatch,
):
    # A terminal recovery run whose durable lineage points at a resumable
    # source must NOT spawn (terminal resume never spawns); it returns
    # ResumeBlockedResult pointing at the source. Spy proves resume is never
    # called.
    write_run(
        fake_workspace, "20260101_000001",
        meta=meta(
            status="failed", project="/p/x", task="source",
            worktree=_RETAINED_WORKTREE,
        ),
    )
    write_run(
        fake_workspace, "20260101_000002",
        meta=meta(
            status="halted", project="/p/x", task="recovery",
            halt_reason="phase_handoff_halt",
            resume_mode="followup", parent_run_id="20260101_000001",
        ),
        supervisor_state=_controllable("20260101_000002"),
    )
    fake = _SpySupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    result = await resume_run("20260101_000002")

    assert isinstance(result, ResumeBlockedResult)
    assert result.resume_outcome == "recover_via_source_run"
    assert result.recommended_run_id == "20260101_000001"
    # No spawn fields on this shape.
    assert not hasattr(result, "pid")
    assert len(result.next_actions) == 1
    na = result.next_actions[0]
    assert na.kind == "ready_call"
    assert na.tool == "orcho_run_resume"
    assert na.args == {"run_id": "20260101_000001"}
    # The terminal recovery run was never resumed.
    assert fake.resume_calls == []


@pytest.mark.asyncio
async def test_resumable_run_applies_with_pid(
    fake_workspace, tmp_path, monkeypatch,
):
    write_run(
        fake_workspace, "20260101_000003",
        meta=meta(status="interrupted", project="/p/x", task="t"),
        supervisor_state=_controllable("20260101_000003"),
    )
    fake = _SpySupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    result = await resume_run("20260101_000003", profile="feature")

    assert isinstance(result, RunResumeResult)
    assert result.resume_outcome == "applied"
    assert result.pid == 4321
    assert result.run_dir == str(tmp_path)
    assert result.started_at == "2026-06-19T12:00:00.000Z"
    assert result.command[-1] == "--resume"
    # The supervisor actually spawned the resume subprocess.
    assert fake.resume_calls == [
        {"run_id": "20260101_000003", "profile": "feature"},
    ]
    # The applied resume points the client back into the watch loop with a
    # ready, pre-filled orcho_run_watch call.
    na = result.suggested_next_action
    assert na is not None
    assert na.kind == "ready_call"
    assert na.tool == "orcho_run_watch"
    assert na.args == {"run_id": "20260101_000003"}
    assert na.requires_operator_input is False


@pytest.mark.asyncio
async def test_non_terminal_halted_still_applies(
    fake_workspace, tmp_path, monkeypatch,
):
    # A non-terminal halt reason must remain resumable (applied), not be
    # mistaken for a terminal no-op.
    write_run(
        fake_workspace, "20260101_000004",
        meta=meta(
            status="halted", project="/p/x", task="t",
            halt_reason="pre_run_dirty_halt",
        ),
        supervisor_state=_controllable("20260101_000004"),
    )
    fake = _SpySupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    result = await resume_run("20260101_000004")

    assert isinstance(result, RunResumeResult)
    assert result.resume_outcome == "applied"
    assert fake.resume_calls and fake.resume_calls[0]["run_id"] == "20260101_000004"


@pytest.mark.asyncio
async def test_final_acceptance_rejected_terminal_without_spawn(
    fake_workspace, tmp_path, monkeypatch,
):
    # F2: a run halted on ``final_acceptance_rejected`` (rejected release, no
    # applied delivery, no correction gate) is a terminal dead-end now that the
    # replica ``_TERMINAL_HALT_REASONS`` mirrors the T1 core vocabulary. The
    # pre-flight must classify it ``rejected_terminal`` BEFORE the supervisor is
    # touched — no spawn fields, no ready ``orcho_run_resume`` action, and the
    # spy proves resume was never called. Shape mirrors run
    # 20260626_165338_90fb22.
    write_run(
        fake_workspace, "20260101_000005",
        meta=meta(
            status="halted", project="/p/x", task="t",
            halt_reason="final_acceptance_rejected",
            phases={"final_acceptance": {"verdict": "REJECTED", "approved": False}},
        ),
        supervisor_state=_controllable("20260101_000005"),
    )
    fake = _SpySupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    result = await resume_run("20260101_000005")

    assert isinstance(result, ResumeBlockedResult)
    assert result.resume_outcome == "rejected_terminal"
    assert result.run_id == "20260101_000005"
    # No spawn fields on this shape.
    assert not hasattr(result, "pid")
    # Inspection-only follow-ups; never a ready resume action.
    assert result.next_actions
    assert all(na.tool != "orcho_run_resume" for na in result.next_actions)
    # The supervisor was never asked to resume the rejected terminal run.
    assert fake.resume_calls == []


# ── Control guard (T3): foreign / CLI-started run dir → inspect_only ─────────
#
# A run dir without a durable ``mcp_supervisor.json`` (only ``meta.json``) was
# not started by this MCP server. The control guard must refuse resume by
# *raising* InspectOnlyControlError BEFORE the supervisor is touched, even when
# the run would otherwise be resumable. Raising (not returning a success-union
# member) keeps orcho_run_resume's success outputSchema unchanged. The carried
# ``result`` is the typed InspectOnlyControlResult: the CLI-control instruction
# rides only in message / suggested_next_action; next_actions stay read-only
# MCP inspection.


def _assert_inspect_only_shape(result, run_id: str) -> None:
    assert isinstance(result, InspectOnlyControlResult)
    assert result.kind == "inspect_only"
    assert result.control == "inspect_only"
    assert result.attempted == "resume"
    assert result.run_id == run_id
    # No spawn fields on this shape.
    assert not hasattr(result, "pid")
    assert not hasattr(result, "run_dir")
    assert not hasattr(result, "command")
    # The CLI-control instruction lives in free text only.
    assert "CLI" in result.message
    assert "CLI" in result.suggested_next_action
    # next_actions are read-only MCP inspection ONLY — every record a ready_call
    # to status / evidence, none a CLI tool, none a resume of this run.
    assert result.next_actions
    assert all(na.kind == "ready_call" for na in result.next_actions)
    assert {na.tool for na in result.next_actions} == {
        "orcho_run_status", "orcho_run_evidence",
    }
    assert all(na.tool != "orcho_run_resume" for na in result.next_actions)
    _assert_inspect_next_action_args(result.next_actions, run_id)


def _assert_inspect_next_action_args(next_actions, run_id: str) -> None:
    # Each record is a valid ready_call to the inspection tool it names: status
    # carries just the run_id; evidence MUST pin slice='errors' (the read-only
    # error slice), so a builder regression that drops or changes the slice is
    # caught here rather than silently passing the name-only checks.
    for na in next_actions:
        assert na.args.get("run_id") == run_id
        if na.tool == "orcho_run_evidence":
            assert na.args.get("slice") == "errors", na.args


@pytest.mark.asyncio
async def test_foreign_resumable_run_is_inspect_only_without_spawn(
    fake_workspace, tmp_path, monkeypatch,
):
    # An otherwise-resumable run (interrupted) but foreign: NO mcp_supervisor.json
    # → control='inspect_only'. The guard fires before the supervisor.
    write_run(
        fake_workspace, "20260101_000010",
        meta=meta(status="interrupted", project="/p/x", task="t"),
    )
    fake = _SpySupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    with pytest.raises(InspectOnlyControlError) as exc:
        await resume_run("20260101_000010")

    _assert_inspect_only_shape(exc.value.result, "20260101_000010")
    # The supervisor was never asked to resume a foreign run.
    assert fake.resume_calls == []


@pytest.mark.asyncio
async def test_foreign_paused_run_is_inspect_only_not_pending_decision(
    fake_workspace, tmp_path, monkeypatch,
):
    # A foreign run paused on a phase handoff must short-circuit to inspect_only
    # BEFORE the pending-decision guard — MCP could never apply a decide-then-
    # resume on a run it did not start.
    write_run(
        fake_workspace, "20260101_000011",
        meta=meta(
            status="awaiting_phase_handoff", project="/p/x", task="t",
            phase_handoff={
                "id": "validate_plan:plan_round:1",
                "phase": "validate_plan",
                "available_actions": ["continue", "halt"],
            },
        ),
    )
    fake = _SpySupervisor(tmp_path)
    _patch_supervisor(monkeypatch, fake)

    with pytest.raises(InspectOnlyControlError) as exc:
        await resume_run("20260101_000011")

    _assert_inspect_only_shape(exc.value.result, "20260101_000011")
    assert fake.resume_calls == []
