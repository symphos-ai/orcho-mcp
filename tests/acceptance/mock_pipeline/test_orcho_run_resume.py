"""Acceptance tests for `orcho_run_resume`.

Pins the resume contract laid down in ``docs/run_lifecycle.md`` against
real subprocess behaviour. The two semantic claims under test:

  * Resume of a genuinely *resumable* run (paused awaiting a decision /
    ``interrupted`` / ``failed`` / a non-terminal ``halted``) is a
    continuation, not a sibling spawn: the same run_id and run_dir are
    reused; the existing checkpoint is loaded into a fresh subprocess;
    runner.log accretes; mcp_supervisor.json is rewritten with a new
    pid + started_at. Covered by
    ``test_resume_applied_continuation_preserves_identity`` (and the
    explicit-profile override variant).

  * Resume of a *terminal* ``done`` run is inert and is refused by the
    pre-flight guard *before* any spawn: it returns a typed
    ``ResumeBlockedResult(resume_outcome='rejected_terminal')`` carrying
    no spawn fields and only read-only inspection next_actions — never a
    fresh subprocess. Covered by
    ``test_resume_terminal_done_returns_rejected_terminal_block``.

Marked ``mcp_integration`` so the default suite stays fast; enable
with ``pytest -m mcp_integration``.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.mcp_integration


# ── fixtures: ``_supervisor_reset``, ``mock_project``, ``runs_dir_of`` ──
# live in the acceptance conftest.


async def _wait_terminal(run_id: str, timeout_s: float = 60.0) -> str:
    """Poll ``orcho_run_status`` until a terminal state is reached."""
    from orcho_mcp.tools import orcho_run_status

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = orcho_run_status(run_id)
        cur = (snap.meta or {}).get("status")
        if cur in {"done", "failed", "interrupted", "halted",
                   "awaiting_phase_handoff"}:
            return cur
        await asyncio.sleep(0.3)
    raise AssertionError(
        f"run {run_id} did not reach terminal status within {timeout_s}s"
    )


async def _wait_status(
    run_id: str, wanted: set[str], timeout_s: float = 90.0,
) -> str:
    """Poll ``orcho_run_status`` until ``meta.status`` lands in ``wanted``."""
    from orcho_mcp.tools import orcho_run_status

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = orcho_run_status(run_id)
        cur = (snap.meta or {}).get("status")
        if cur in wanted:
            return cur
        await asyncio.sleep(0.3)
    raise AssertionError(
        f"run {run_id} did not reach {sorted(wanted)} within {timeout_s}s"
    )


# The deterministic implement-incomplete handoff id minted by core's
# subtask_dag (see test_implement_incomplete_handoff.py).
_RESUMABLE_HANDOFF_ID = "implement:implement_handoff:1"


async def _build_resumable_run(
    mock_project: Path, monkeypatch: pytest.MonkeyPatch, *, profile: str = "feature",
):
    """Drive a feature run to a genuinely *resumable* pause, then return it.

    Resuming a terminal ``done`` run is a no-op the pre-flight guard refuses
    (it returns ``ResumeBlockedResult`` with no spawn fields). To exercise the
    real ``applied`` spawn we need a run that is paused-but-decided — the one
    state where ``orcho_run_resume`` genuinely launches a continuation.

    The deterministic path (same as
    ``test_implement_incomplete_handoff.py``): arm
    ``ORCHO_MOCK_IMPLEMENT_INCOMPLETE`` *before* spawn so the subprocess
    inherits it (``spawn.py`` does ``os.environ.copy()``). The mock then
    leaves a subtask criterion unmet, the implement delivery resolves to
    ``incomplete``, and — with ``auto_waiver_allowed`` at its default
    ``False`` — the run PAUSES on the implement handoff instead of
    auto-waiving. The operator records ``continue_with_waiver``, leaving the
    run paused-but-decided and ready for an ``applied`` resume.

    Returns the original ``RunStartedResult`` so callers can pin run identity
    (``run_id`` / ``run_dir`` / ``pid``) across the resume.
    """
    from orcho_mcp.tools import orcho_phase_handoff_decide, orcho_run_start

    monkeypatch.setenv("ORCHO_MOCK_IMPLEMENT_INCOMPLETE", "1")
    started = await orcho_run_start(
        task="resume genuinely-resumable smoke",
        project_dir=str(mock_project),
        mock=True,
        profile=profile,
        max_rounds=1,
    )
    assert (await _wait_status(started.run_id, {"awaiting_phase_handoff"})) == (
        "awaiting_phase_handoff"
    )
    await orcho_phase_handoff_decide(
        started.run_id,
        handoff_id=_RESUMABLE_HANDOFF_ID,
        action="continue_with_waiver",
        feedback="accept: ship with stub",
    )
    return started


# ── happy path ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_applied_continuation_preserves_identity(
    mock_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely resumable run spawns a continuation that keeps run identity.

    Consolidates the positive meaning of the former
    ``reuses_run_id_and_run_dir`` / ``passes_resume_flag_and_appends_to_
    runner_log`` / ``rewrites_supervisor_state_with_fresh_pid`` assertions
    onto a run that actually spawns (paused-but-decided), instead of a
    terminal ``done`` run that the pre-flight guard refuses:

      * typed ``RunResumeResult`` with ``resume_outcome='applied'``;
      * same ``run_id`` + ``run_dir``, fresh ``pid`` (new OS process);
      * ``--resume <run_id>`` in argv, with ``--profile feature`` inherited
        from the original run's meta;
      * ``runner.log`` grows and carries the supervisor's ``=== resume @``
        marker (operational history accretes across the spawn);
      * ``mcp_supervisor.json`` is rewritten with the fresh pid / started_at
        so a server restart recovers the *live* subprocess, not the dead one.
    """
    from orcho_mcp.schemas import RunResumeResult
    from orcho_mcp.tools import orcho_run_resume

    started = await _build_resumable_run(mock_project, monkeypatch)
    run_id = started.run_id
    original_run_dir = Path(started.run_dir)

    runner_log = original_run_dir / "runner.log"
    log_size_before = runner_log.stat().st_size if runner_log.exists() else 0
    state_path = original_run_dir / "mcp_supervisor.json"
    state_pre = json.loads(state_path.read_text(encoding="utf-8"))
    pid_pre = state_pre["pid"]
    started_at_pre = state_pre["started_at"]

    resumed = await orcho_run_resume(run_id)

    # Typed applied outcome — a real spawn, not a pre-flight block.
    assert isinstance(resumed, RunResumeResult), type(resumed)
    assert resumed.resume_outcome == "applied"

    # Same on-disk run; fresh OS process owns it.
    assert resumed.run_id == run_id
    assert Path(resumed.run_dir) == original_run_dir
    assert resumed.pid != started.pid

    # argv drives ``--resume <run_id>`` and inherits ``--profile feature``.
    assert "--resume" in resumed.command, resumed.command
    ridx = resumed.command.index("--resume")
    assert resumed.command[ridx + 1] == run_id
    assert "--profile" in resumed.command, resumed.command
    pidx = resumed.command.index("--profile")
    assert resumed.command[pidx + 1] == "feature", (
        f"resume must inherit --profile feature from meta; "
        f"got {resumed.command[pidx + 1]!r}"
    )

    # Supervisor state is rewritten to point at the live (resumed) subprocess
    # — capture it before the run reaps so the assertion is on the fresh spawn.
    state_post = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_post["run_id"] == run_id
    assert state_post["pid"] == resumed.pid
    assert state_post["pid"] != pid_pre, "supervisor state still holds dead pid"
    assert state_post["started_at"] != started_at_pre, (
        "started_at not refreshed by resume"
    )

    # Let the resumed subprocess finish so runner.log flushes.
    assert (await _wait_status(run_id, {"done", "failed", "halted"})) == "done"

    # Operational history accretes across the spawn.
    log_size_after = runner_log.stat().st_size
    assert log_size_after > log_size_before, (
        f"runner.log did not grow across resume "
        f"({log_size_before} → {log_size_after})"
    )
    assert "=== resume @" in runner_log.read_text(encoding="utf-8"), (
        "supervisor's resume marker not present in runner.log"
    )


# ── terminal resume is refused before spawn ─────────────────────────────────


@pytest.mark.asyncio
async def test_resume_terminal_done_returns_rejected_terminal_block(
    mock_project: Path,
) -> None:
    """Resuming a terminal ``done`` run is inert and is refused *before* any
    spawn.

    This pins the negative half of the resume contract (the inverse of
    ``test_resume_applied_continuation_preserves_identity``): a ``done`` run
    cannot be advanced by resume, so ``orcho_run_resume`` returns a typed
    :class:`ResumeBlockedResult` (``resume_outcome='rejected_terminal'``)
    rather than a spawn handle. The block:

      * is the typed refusal shape (``kind='resume_blocked'``), names the
        same ``run_id``, and has no resume target (``recommended_run_id is
        None``);
      * carries **no spawn fields** — asserted against the public wire form
        (``model_dump()`` has no ``pid`` / ``run_dir`` / ``command`` /
        ``started_at`` keys), not via ``hasattr``;
      * points only at read-only inspection (``orcho_run_status`` /
        ``orcho_run_evidence``); no ``next_action`` re-invokes
        ``orcho_run_resume``;
      * spawned nothing — no new sibling run dir, runner.log did not grow,
        and mcp_supervisor.json is byte-for-byte unchanged.

    Consolidates the negative meaning of the former
    ``reuses_run_id_and_run_dir`` / ``passes_resume_flag_and_appends_to_
    runner_log`` / ``rewrites_supervisor_state_with_fresh_pid`` tests, which
    had assumed (now incorrectly) that resuming a terminal ``done`` run
    spawns a continuation.
    """
    from orcho_mcp.schemas import ResumeBlockedResult
    from orcho_mcp.tools import orcho_run_resume, orcho_run_start

    started = await orcho_run_start(
        task="resume terminal done is inert",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    run_id = started.run_id
    run_dir = Path(started.run_dir)
    runs_root = run_dir.parent

    assert (await _wait_terminal(run_id)) == "done"

    # Snapshot the on-disk state so we can prove nothing was spawned.
    sibling_count_before = sum(1 for _ in runs_root.iterdir())
    runner_log = run_dir / "runner.log"
    log_size_before = runner_log.stat().st_size if runner_log.exists() else 0
    state_path = run_dir / "mcp_supervisor.json"
    state_bytes_before = state_path.read_bytes()

    resumed = await orcho_run_resume(run_id, profile="task")

    # ── typed refusal contract ──
    assert isinstance(resumed, ResumeBlockedResult), type(resumed)
    assert resumed.kind == "resume_blocked"
    assert resumed.resume_outcome == "rejected_terminal"
    assert resumed.run_id == run_id
    assert resumed.recommended_run_id is None

    # No spawn fields in the public wire form (not via hasattr).
    dumped = resumed.model_dump()
    assert {"pid", "run_dir", "command", "started_at"}.isdisjoint(dumped), (
        f"rejected_terminal block must carry no spawn fields; got keys "
        f"{sorted(dumped)}"
    )

    # next_actions point only at read-only inspection — never a resume — and
    # each is a forwardable ``ready_call`` carrying the exact required args
    # (this pins the public guidance contract, not just the tool names).
    tools = [na.tool for na in resumed.next_actions]
    assert tools, "rejected_terminal block must offer inspection next_actions"
    assert set(tools) <= {"orcho_run_status", "orcho_run_evidence"}, tools
    assert "orcho_run_resume" not in tools

    by_tool = {na.tool: na for na in resumed.next_actions}
    status_action = by_tool.get("orcho_run_status")
    assert status_action is not None, resumed.next_actions
    assert status_action.kind == "ready_call"
    assert status_action.args == {"run_id": run_id}

    evidence_action = by_tool.get("orcho_run_evidence")
    assert evidence_action is not None, resumed.next_actions
    assert evidence_action.kind == "ready_call"
    assert evidence_action.args == {"run_id": run_id, "slice": "errors"}

    # ── nothing was spawned ──
    sibling_count_after = sum(1 for _ in runs_root.iterdir())
    assert sibling_count_after == sibling_count_before, (
        f"refused resume must not mint a sibling run dir "
        f"(before={sibling_count_before}, after={sibling_count_after})"
    )
    log_size_after = runner_log.stat().st_size if runner_log.exists() else 0
    assert log_size_after == log_size_before, (
        f"refused resume must not grow runner.log "
        f"({log_size_before} → {log_size_after})"
    )
    assert state_path.read_bytes() == state_bytes_before, (
        "refused resume must not rewrite mcp_supervisor.json"
    )


# ── error contracts ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_unknown_run_id_raises_run_not_found(
    mock_project: Path,
) -> None:
    """Resume against a non-existent run_id must raise the structured
    ``RunNotFoundError`` — not a raw OSError or generic exception."""
    from orcho_mcp.errors import RunNotFoundError
    from orcho_mcp.tools import orcho_run_resume

    with pytest.raises(RunNotFoundError) as exc_info:
        await orcho_run_resume("does_not_exist_20990101_000000", profile="task")
    msg = str(exc_info.value)
    assert "does_not_exist_20990101_000000" in msg


@pytest.mark.asyncio
async def test_resume_run_dir_without_supervisor_state_is_inspect_only(
    mock_project: Path,
    runs_dir: Path,
) -> None:
    """A run dir without ``mcp_supervisor.json`` is ``inspect_only`` — MCP did
    not start it, so the control guard refuses resume *before* the supervisor by
    raising :class:`InspectOnlyControlError` (carrying the typed
    :class:`InspectOnlyControlResult`, ``attempted='resume'``) rather than a late
    ``RunNotFoundError``. Raising — instead of returning a success-union member —
    keeps ``orcho_run_resume``'s success ``outputSchema`` unchanged. The durable
    boundary is documented in ``docs/run_lifecycle.md``; the full diagnose +
    decide coverage lives in ``test_foreign_run_control_boundary.py``. This pins
    the resume arm for a run dir that carries no durable supervisor state at
    all."""
    from orcho_mcp.errors import InspectOnlyControlError
    from orcho_mcp.schemas import InspectOnlyControlResult
    from orcho_mcp.tools import orcho_run_resume

    bare_run_id = "20990102_030405_bareee"
    bare_run_dir = runs_dir / bare_run_id
    bare_run_dir.mkdir(parents=True)
    # Deliberately *no* mcp_supervisor.json — the durable controllability
    # signal — so the run classifies inspect_only and resume is refused.

    with pytest.raises(InspectOnlyControlError) as exc:
        await orcho_run_resume(bare_run_id, profile="task")

    resumed = exc.value.result
    assert isinstance(resumed, InspectOnlyControlResult), type(resumed)
    assert resumed.kind == "inspect_only"
    assert resumed.control == "inspect_only"
    assert resumed.attempted == "resume"
    assert resumed.run_id == bare_run_id
    # No spawn fields in the public wire form.
    dumped = resumed.model_dump()
    assert {"pid", "run_dir", "command", "started_at"}.isdisjoint(dumped), (
        f"inspect_only resume must carry no spawn fields; got {sorted(dumped)}"
    )
    # Read-only inspection next_actions only; never a resume of this run.
    tools = {na.tool for na in resumed.next_actions}
    assert tools <= {"orcho_run_status", "orcho_run_evidence"}, tools
    assert "orcho_run_resume" not in tools
    # The CLI-control instruction rides in free text, not a next_action.
    assert "CLI" in resumed.message


# ── fresh-vs-resume separation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_start_after_resume_mints_new_run_id(
    mock_project: Path,
) -> None:
    """A second ``orcho_run_start`` must mint a *new* run_id even if a
    prior run on the same project just finished + was resumed. Resume
    must not poison the start-side run-id minting (regression guard
    against accidental "reuse last id" semantics)."""
    from orcho_mcp.tools import orcho_run_resume, orcho_run_start

    first = await orcho_run_start(
        task="first run",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    assert (await _wait_terminal(first.run_id)) == "done"
    # Resuming a terminal ``done`` run is now a refused no-op
    # (``ResumeBlockedResult(rejected_terminal)``); it neither spawns nor
    # mutates run-id minting, which is exactly what the second start guards.
    await orcho_run_resume(first.run_id, profile="task")
    assert (await _wait_terminal(first.run_id)) == "done"

    second = await orcho_run_start(
        task="second run after resume",
        project_dir=str(mock_project),
        mock=True,
        max_rounds=1,
    )
    assert second.run_id != first.run_id
    assert Path(second.run_dir) != Path(first.run_dir)
    assert (await _wait_terminal(second.run_id)) == "done"


# ── profile override semantic ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_with_explicit_profile_overrides_meta(
    mock_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``profile="<name>"`` on ``orcho_run_resume`` must override
    ``meta.profile`` — the deliberate-switch path, e.g. resume a ``feature``
    run into the leaner ``small_task`` profile.

    Runs on a genuinely *resumable* run (paused-but-decided) so the resume
    actually spawns and the override lands in the subprocess argv. The
    inherit path (no ``profile`` arg → ``--profile feature`` from meta) is
    already covered by
    ``test_resume_applied_continuation_preserves_identity``; this is the
    inverse — an explicit profile must win over the inherited
    ``meta.profile='feature'``.
    """
    from orcho_mcp.schemas import RunResumeResult
    from orcho_mcp.tools import orcho_run_resume

    started = await _build_resumable_run(mock_project, monkeypatch)

    resumed = await orcho_run_resume(started.run_id, profile="small_task")

    assert isinstance(resumed, RunResumeResult), type(resumed)
    assert resumed.resume_outcome == "applied"
    assert "--profile" in resumed.command, resumed.command
    pidx = resumed.command.index("--profile")
    assert resumed.command[pidx + 1] == "small_task", (
        f"explicit --profile small_task must win over meta.profile=feature; "
        f"got {resumed.command[pidx + 1]!r}"
    )

    # Drain the resumed subprocess so it does not outlive the test.
    await _wait_terminal(started.run_id)
