"""Synthetic workspace + runs-directory fixtures for orcho-mcp tests.

Provides ``fake_workspace`` (pytest fixture), ``write_run`` (plain
helper), and a set of scenario builders (``meta``, ``metrics``,
``event``, ``supervisor_state``) so unit and L3 tests can exercise
read-only tooling without touching a real orcho install. The fixture
sets ``ORCHO_WORKSPACE`` for the test's lifetime —
``core.infra.config`` reads that on every call, so all the
workspace helpers (``get_workspace_dir``, ``get_runs_dir``, etc.)
point at the temp tree.

The builders return plain dicts matching the well-known on-disk
shapes (``meta.json``, ``metrics.json``, one ``events.jsonl`` line,
``mcp_supervisor.json``). They are intentionally dumb — no
filesystem IO, no hidden assertions, defaults match the most common
"happy-path" run, and ``**extra`` is the escape hatch for fields
the builder doesn't enumerate explicitly. Compose builders inside
``write_run`` calls:

    write_run(
        fake_workspace, "run_001",
        meta=meta(status="done"),
        metrics=metrics(total_tokens=500),
        events=[event(1, "run.start"), event(2, "run.end")],
    )

This module is imported via ``tests/conftest.py`` (root) which
re-exports ``fake_workspace`` for pytest's normal fixture discovery,
and via direct ``from tests.fixtures.mcp_workspace import write_run``
(or the builders) for the helper functions. ``pythonpath = ["."]``
in ``pyproject.toml`` makes the ``tests.fixtures.*`` namespace path
resolvable.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

# Mirror of orcho-core ``pipeline.engine.commit_delivery._safe_decision_id``
# (and the MCP-side replica in ``services.run_artifacts``): the commit-decision
# artifact is keyed by the sanitized run id.
_SAFE_DECISION_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_decision_id(run_id: str) -> str:
    return _SAFE_DECISION_ID_RE.sub("_", run_id).strip("._")


def init_git_repo(path: Path) -> None:
    """Initialize ``path`` as a git repo with one committed file.

    orcho-core's worktree resolver hard-fails when ``project_dir``
    is not a real git checkout (no HEAD), so any supervisor or
    pilot test that hands ``project_dir`` to the engine must use
    this helper rather than ``Path.mkdir`` alone.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@orcho.invalid"],
        cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Orcho Test"],
        cwd=path, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=path, check=True,
    )
    (path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=path, check=True,
    )


@pytest.fixture
def fake_workspace(tmp_path: Path, monkeypatch) -> Path:
    """Create an empty workspace tree and point orcho at it.

    Layout::

        <tmp>/
        └── ws/
            └── runspace/
                └── runs/      (empty; tests populate via ``write_run`` helper)

    The extra ``ws/`` nesting matters: ``cli.orcho._walkup_runs_dir()`` does
    sibling-scan as it walks up the tree, so a runspace sitting directly
    under ``tmp_path`` would be pickable by *other* tests that happen to
    chdir into a sibling pytest tmp under the same pytest run. Nesting one
    level deeper keeps our fixture invisible to walkup and makes the
    explicit ``ORCHO_WORKSPACE`` env var the only resolution path.

    Yields the workspace root. ``ORCHO_WORKSPACE`` is set for the test's
    lifetime; pytest's monkeypatch undoes it on teardown.

    ``ORCHO_RUNSPACE`` is cleared because ``runspace_dir()`` prefers it
    over ``$ORCHO_WORKSPACE/runspace``. When the suite runs inside an
    ambient Orcho run that var is set, which would resolve run discovery
    against the real runspace instead of this synthetic tree — exactly
    the cross-test leakage this fixture's nesting already guards against.
    """
    ws = tmp_path / "ws"
    (ws / "runspace" / "runs").mkdir(parents=True)
    monkeypatch.setenv("ORCHO_WORKSPACE", str(ws))
    monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
    return ws


def in_workspace_project(workspace: Path, name: str = "project") -> str:
    """Create an existing project directory under the workspace root.

    Returns the directory path as a string suitable for ``meta.project``.

    The pending-decisions projector classifies a paused run as ``actionable``
    (default-visible) only when its ``meta.project`` path *exists* and lives
    under the resolved workspace root. ``fake_workspace`` itself is created
    under a pytest temp directory, so seeding the project here is the direct
    way to exercise the *workspace-valid-beats-temp* rule: the project is
    actionable even though the workspace root sits under
    ``/private/var/folders`` / ``pytest-*``.
    """
    proj = workspace / name
    proj.mkdir(parents=True, exist_ok=True)
    return str(proj)


def write_run(
    workspace: Path,
    run_id: str,
    *,
    meta: dict | None = None,
    metrics: dict | None = None,
    events: list[dict] | None = None,
    supervisor_state: dict | None = None,
    meta_text: str | None = None,
    commit_decision: dict | None = None,
    diff_patch: str | None = None,
    parsed_plan: dict | None = None,
    parsed_plan_text: str | None = None,
) -> Path:
    """Create ``<workspace>/runspace/runs/<run_id>/`` with the supplied artefacts.

    Helper, not a fixture — tests call this with whatever subset of files
    they need. Returns the run directory.

    ``meta_text`` writes a raw ``meta.json`` body verbatim (use it to inject
    a corrupt / non-JSON meta; mutually exclusive with ``meta``).
    ``commit_decision`` writes the durable
    ``commit_decisions/<safe_run_id>.json`` audit artifact; ``diff_patch``
    writes the run-level ``diff.patch`` (pass a corrupt body to exercise the
    degraded path). ``parsed_plan`` writes the durable ``parsed_plan.json``
    artifact (and ``parsed_plan_text`` writes a raw, possibly-corrupt body
    verbatim). All default to absent so a test can omit a secondary artifact
    to exercise the missing-artifact diagnostics.
    """
    run_dir = workspace / "runspace" / "runs" / run_id
    run_dir.mkdir(parents=True)
    if meta is not None:
        (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if meta_text is not None:
        (run_dir / "meta.json").write_text(meta_text, encoding="utf-8")
    if metrics is not None:
        (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    if events is not None:
        (run_dir / "events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n",
            encoding="utf-8",
        )
    if supervisor_state is not None:
        (run_dir / "mcp_supervisor.json").write_text(
            json.dumps(supervisor_state), encoding="utf-8",
        )
    if commit_decision is not None:
        decisions_dir = run_dir / "commit_decisions"
        decisions_dir.mkdir(parents=True, exist_ok=True)
        safe = _safe_decision_id(run_id)
        (decisions_dir / f"{safe}.json").write_text(
            json.dumps(commit_decision), encoding="utf-8",
        )
    if diff_patch is not None:
        (run_dir / "diff.patch").write_text(diff_patch, encoding="utf-8")
    if parsed_plan is not None:
        (run_dir / "parsed_plan.json").write_text(
            json.dumps(parsed_plan), encoding="utf-8",
        )
    if parsed_plan_text is not None:
        (run_dir / "parsed_plan.json").write_text(
            parsed_plan_text, encoding="utf-8",
        )
    return run_dir


# ── Scenario builders ───────────────────────────────────────────────────────
#
# Each builder returns a plain dict matching the well-known on-disk
# shape of one artefact. Builders are dumb: no filesystem IO, no
# hidden assertions, no validation of field correctness. Defaults
# encode the most common "happy-path" run shape; tests override
# specific fields via keyword args, and ``**extra`` is the escape
# hatch for anything the builder doesn't enumerate.
#
# Update protocol: when a new field becomes commonly read by tests,
# promote it from ``**extra`` to a named keyword argument here. The
# default should match what a real run carries when nothing
# interesting is happening.


def meta(
    *,
    status: str = "running",
    task: str = "test task",
    project: str = "/tmp/project",
    profile: str = "feature",
    **extra: Any,
) -> dict[str, Any]:
    """Build a ``meta.json`` dict.

    The four named kwargs cover the fields every read path inspects
    routinely. Common additions to drop in via ``**extra``:
    ``phase``, ``halt_reason``, ``halted_at``, ``phase_handoff``,
    ``current_phase``, ``timestamp``.
    """
    out: dict[str, Any] = {
        "status": status,
        "task": task,
        "project": project,
        "profile": profile,
    }
    out.update(extra)
    return out


def metrics(
    *,
    total_tokens: int = 123,
    total_duration_s: float = 1.0,
    rounds: int = 1,
    phases: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a ``metrics.json`` dict.

    ``phases`` is left ``None`` by default — tests that exercise
    per-phase breakdowns pass an explicit dict. The minimal shape
    here is what ``orcho_run_history`` reads.
    """
    out: dict[str, Any] = {
        "total_tokens": total_tokens,
        "total_duration_s": total_duration_s,
        "rounds": rounds,
    }
    if phases is not None:
        out["phases"] = phases
    out.update(extra)
    return out


def event(
    seq: int,
    kind: str,
    *,
    phase: str | None = None,
    payload: dict[str, Any] | None = None,
    ts: str = "2026-01-01T00:00:00.000Z",
) -> dict[str, Any]:
    """Build one ``events.jsonl`` line.

    The on-disk shape is ``{seq, ts, kind, phase, payload}``. ``phase``
    defaults to ``None`` (events without a phase tag are valid —
    e.g. ``run.start``, ``run.end``). ``payload`` defaults to an
    empty dict; tests that exercise payload-aware paths pass an
    explicit shape.
    """
    return {
        "seq": seq,
        "ts": ts,
        "kind": kind,
        "phase": phase,
        "payload": payload or {},
    }


def supervisor_state(
    *,
    run_id: str,
    pid: int = 999_999,
    pgid: int | None = None,
    status: str = "running",
    project_dir: str = "/tmp/project",
    started_at: str = "2026-01-01T00:00:00.000Z",
    **extra: Any,
) -> dict[str, Any]:
    """Build a ``mcp_supervisor.json`` dict.

    Defaults to a "running" record with a high pid that is reliably
    dead on a real system (useful for orphan-recovery tests).
    ``pgid`` defaults to ``pid`` (the supervisor uses
    ``start_new_session=True`` so the child is its own process
    group; pgid == pid is the realistic shape).

    Common additions via ``**extra``: ``halt_reason``, ``exit_code``,
    ``mock``, ``output_mode``.
    """
    out: dict[str, Any] = {
        "run_id": run_id,
        "pid": pid,
        "pgid": pgid if pgid is not None else pid,
        "status": status,
        "project_dir": project_dir,
        "started_at": started_at,
    }
    out.update(extra)
    return out


def launch_state(
    *,
    run_id: str,
    pid: int = 999_999,
    pgid: int | None = None,
    status: str = "running",
    project_dir: str = "/tmp/project",
    started_at: str = "2026-01-01T00:00:00.000Z",
    command: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a neutral ``run_supervisor.json`` dict (the SDK launch state).

    Companion to :func:`supervisor_state`. ``launch_run`` / ``resume_run``
    write this file at spawn; ``cancel_run`` reads pid / pgid back from it.
    Tests that drive the *owned* (already-consistent) cancel path — where
    both state files exist and no materialisation bridge is needed — write
    this alongside ``mcp_supervisor.json``. Orphan-bridge tests deliberately
    omit it so the cancel path materialises it from the MCP delta.

    Mirrors ``sdk.run_control.launch.write_launch_state``'s payload shape.
    """
    out: dict[str, Any] = {
        "run_id": run_id,
        "pid": pid,
        "pgid": pgid if pgid is not None else pid,
        "command": command if command is not None else ["x"],
        "project_dir": project_dir,
        "started_at": started_at,
        "status": status,
        "mock": False,
        "output_mode": "summary",
    }
    out.update(extra)
    return out


def commit_delivery(
    *,
    status: str = "pending",
    action: str = "approve",
    release_verdict: str = "APPROVED",
    changed_paths: list[str] | None = None,
    untracked_paths: list[str] | None = None,
    project_path: str = "/tmp/project",
    source_path: str = "/tmp/worktree",
    **extra: Any,
) -> dict[str, Any]:
    """Build a ``meta['commit_delivery']`` decision dict.

    Mirrors the persisted ``CommitDeliveryDecision.to_dict()`` shape. The
    authoritative gate key is ``status`` (NOT ``commit_status``) — the
    builder writes ``status`` so the projection's classification reads the
    same key core persists. Drop into ``meta(...)`` as the
    ``commit_delivery`` field.
    """
    out: dict[str, Any] = {
        "status": status,
        "action": action,
        "release_verdict": release_verdict,
        "project_path": project_path,
        "source_path": source_path,
        "changed_paths": (
            changed_paths if changed_paths is not None else ["src/a.py"]
        ),
        "untracked_paths": untracked_paths if untracked_paths is not None else [],
    }
    out.update(extra)
    return out


def commit_decision(
    *,
    action: str = "approve",
    commit_status: str = "pending",
    files_staged: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a ``commit_decisions/<id>.json`` audit artifact dict.

    The artifact's status key is ``commit_status`` (the non-authoritative
    legacy alias) — distinct from the authoritative ``status`` on the meta
    decision. This artifact only enriches the diff summary.
    """
    out: dict[str, Any] = {
        "action": action,
        "commit_status": commit_status,
        "files_staged": files_staged if files_staged is not None else ["src/a.py"],
    }
    out.update(extra)
    return out


def diff_patch_text(*paths: str) -> str:
    """Build a minimal but valid unified ``diff.patch`` body for ``paths``.

    Defaults to a single-file patch when no paths are given.
    """
    files = paths or ("src/a.py",)
    chunks: list[str] = []
    for p in files:
        chunks.append(
            f"diff --git a/{p} b/{p}\n"
            f"--- a/{p}\n"
            f"+++ b/{p}\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n",
        )
    return "".join(chunks)


__all__ = [
    "commit_decision",
    "commit_delivery",
    "diff_patch_text",
    "event",
    "fake_workspace",
    "in_workspace_project",
    "launch_state",
    "meta",
    "metrics",
    "supervisor_state",
    "write_run",
]
