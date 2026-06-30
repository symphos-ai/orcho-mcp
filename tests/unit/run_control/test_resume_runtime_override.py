"""``orcho_run_resume`` runtime_override delivery.

The provider-access recovery *replace* Action targets ``orcho_run_resume`` with
``runtime_override={phase, runtime, model}``. For that Action to actually
deliver the operator's choice, the MCP tool must (1) accept the arg through its
strict schema and (2) fix the override into the run's durable ``meta.json``
*before* the supervisor spawns the resume subprocess — the subprocess re-reads
that record and applies it. These mock-smokes prove the MCP-side wiring end to
end: the tool accepts the typed arg, persistence runs before the spawn, and a
non-candidate pair aborts the resume as a typed bad-request.

orcho-core stays the single validation + persistence authority. Its own unit
suite (``tests/unit/pipeline/project/test_runtime_override_resume.py`` in
orcho-core) pins ``persist_runtime_override``'s real candidate validation,
idempotency, conflict, and resume carry-forward. Because the orcho-mcp test
environment resolves ``sdk`` against the installed orcho-core (which predates
the new module until promotion), these smokes install a stand-in for the
orcho-core persistence boundary that mirrors its contract — they assert the
*wiring*, not orcho-core's internal logic.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from orcho_mcp.errors import InvalidPlanError
from orcho_mcp.run_control.lifecycle import resume_run
from orcho_mcp.schemas import RunResumeResult, RuntimeOverrideArg
from orcho_mcp.supervisor import RunHandle
from tests.fixtures.mcp_workspace import meta, supervisor_state, write_run

# T3 control guard: resume_run refuses runs MCP did not start (no durable
# mcp_supervisor.json) by raising InspectOnlyControlError before any spawn. These
# runtime-override tests exercise the mcp_controllable applied/abort paths, so
# each inspected run carries durable supervisor state with a resolvable
# project_dir.


def _controllable_state(run_id: str):
    return supervisor_state(run_id=run_id, project_dir="/p/x")


class _SpySupervisor:
    """Fake supervisor recording whether ``resume`` was ever invoked."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.resume_calls: list[dict] = []

    async def resume(self, run_id: str, *, profile: str | None = None):
        self.resume_calls.append({"run_id": run_id, "profile": profile})
        return RunHandle(
            run_id=run_id,
            pid=4321,
            pgid=4321,
            run_dir=self.run_dir,
            project_dir="/p",
            command=["python", "-m", "pipeline.project_orchestrator", "--resume"],
            started_at="2026-06-19T12:00:00.000Z",
        )


def _patch_supervisor(monkeypatch, fake) -> None:
    monkeypatch.setattr("orcho_mcp.supervisor.get_supervisor", lambda: fake)


def _install_core_override_stub(monkeypatch, candidates: list[dict]) -> None:
    """Install a stand-in for orcho-core's ``sdk.run_control.runtime_override``.

    ``orcho_mcp.run_control.lifecycle._persist_runtime_override`` lazily imports
    ``persist_runtime_override`` from orcho-core. The stub mirrors the real
    contract: validate the ``(runtime, model)`` pair against ``candidates`` and
    write ``meta['runtime_override']`` (a non-candidate pair raises a
    ``ValueError`` the MCP boundary maps to ``InvalidPlanError``). This isolates
    the MCP wiring from the orcho-core install version while still exercising the
    real run-dir resolution, error mapping, and persist-before-spawn ordering.
    """
    mod = types.ModuleType("sdk.run_control.runtime_override")

    def persist_runtime_override(
        run_dir: Path, *, phase: str, runtime: str, model: str,
        note: str | None = None, decided_at: str | None = None,
    ) -> dict:
        if {"runtime": runtime, "model": model} not in candidates:
            raise ValueError(
                f"runtime override for phase {phase!r}: "
                f"({runtime!r}, {model!r}) is not a configured candidate",
            )
        meta_file = Path(run_dir) / "meta.json"
        on_disk = (
            json.loads(meta_file.read_text(encoding="utf-8"))
            if meta_file.is_file() else {}
        )
        record = {
            "phase": phase, "runtime": runtime, "model": model,
            "decided_at": decided_at or "2026-06-24T00:00:00", "note": note,
        }
        on_disk["runtime_override"] = record
        meta_file.write_text(json.dumps(on_disk), encoding="utf-8")
        return record

    mod.persist_runtime_override = persist_runtime_override
    monkeypatch.setitem(
        sys.modules, "sdk.run_control.runtime_override", mod,
    )


_FAILED_META = meta(
    status="failed", project="/p/x", task="t",
    failure={
        "phase": "implement",
        "failure_kind": "provider_access",
        "failed_phase": "implement",
        "recovery_actions": [
            {"action": "retry"},
            {"action": "halt"},
            {"action": "replace", "runtime": "codex", "model": "gpt-5"},
        ],
    },
)


@pytest.mark.asyncio
async def test_runtime_override_persisted_before_spawn(
    fake_workspace, monkeypatch,
):
    run_dir = write_run(
        fake_workspace, "20260101_000001", meta=_FAILED_META,
        supervisor_state=_controllable_state("20260101_000001"),
    )
    fake = _SpySupervisor(run_dir)
    _patch_supervisor(monkeypatch, fake)
    _install_core_override_stub(monkeypatch, [{"runtime": "codex", "model": "gpt-5"}])

    result = await resume_run(
        "20260101_000001",
        runtime_override=RuntimeOverrideArg(
            phase="implement", runtime="codex", model="gpt-5",
        ),
    )

    # The resume spawned a real subprocess (success-shaped).
    assert isinstance(result, RunResumeResult)
    assert result.resume_outcome == "applied"
    assert fake.resume_calls == [
        {"run_id": "20260101_000001", "profile": None},
    ]
    # The operator's override is durable in meta.json — written before the
    # supervisor was asked to spawn (the resumed pipeline re-reads + applies it).
    persisted = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    record = persisted["runtime_override"]
    assert record["phase"] == "implement"
    assert record["runtime"] == "codex"
    assert record["model"] == "gpt-5"


@pytest.mark.asyncio
async def test_plain_resume_writes_no_override(fake_workspace, monkeypatch):
    run_dir = write_run(
        fake_workspace, "20260101_000002",
        meta=meta(status="interrupted", project="/p/x", task="t"),
        supervisor_state=_controllable_state("20260101_000002"),
    )
    fake = _SpySupervisor(run_dir)
    _patch_supervisor(monkeypatch, fake)

    result = await resume_run("20260101_000002")

    assert isinstance(result, RunResumeResult)
    assert fake.resume_calls and fake.resume_calls[0]["run_id"] == "20260101_000002"
    # No override arg → meta.json carries no runtime_override key (behaviour
    # unchanged for a plain resume).
    persisted = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert "runtime_override" not in persisted


@pytest.mark.asyncio
async def test_non_candidate_override_rejected_without_spawn(
    fake_workspace, monkeypatch,
):
    run_dir = write_run(
        fake_workspace, "20260101_000003", meta=_FAILED_META,
        supervisor_state=_controllable_state("20260101_000003"),
    )
    fake = _SpySupervisor(run_dir)
    _patch_supervisor(monkeypatch, fake)
    # Only ``codex/gpt-5`` is a configured candidate; the operator-supplied
    # pair below is not, so persistence must reject it and resume must abort
    # before the supervisor is ever asked to spawn.
    _install_core_override_stub(monkeypatch, [{"runtime": "codex", "model": "gpt-5"}])

    with pytest.raises(InvalidPlanError):
        await resume_run(
            "20260101_000003",
            runtime_override=RuntimeOverrideArg(
                phase="implement", runtime="gemini", model="ghost-model",
            ),
        )

    # No spawn, and no override written for the rejected pair.
    assert fake.resume_calls == []
    persisted = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert "runtime_override" not in persisted
