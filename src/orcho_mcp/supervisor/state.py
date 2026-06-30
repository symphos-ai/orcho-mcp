"""orcho_mcp.supervisor.state — ``mcp_supervisor.json`` IO + meta probes.

Owns the state-file schema (kept stable; restart-recovery depends on
it) and the small ``meta.json`` probes that resume uses to recover
task/profile after pipeline-side writes finished.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orcho_mcp.supervisor.handle import RunHandle

STATE_FILE = "mcp_supervisor.json"


def now_iso() -> str:
    """ISO-8601 in UTC, e.g. ``2026-05-06T14:30:22.123Z``."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def write_state(handle: RunHandle) -> None:
    """Persist (or update) ``<run_dir>/mcp_supervisor.json``.

    Schema is the public contract of restart-recovery — keep keys stable.
    """
    payload = {
        "run_id":     handle.run_id,
        "pid":        handle.pid,
        "pgid":       handle.pgid,
        "command":    handle.command,
        "cwd":        handle.project_dir,
        "project_dir": handle.project_dir,
        "started_at": handle.started_at,
        "status":     handle.status,
        "mock":       handle.mock,
        "output_mode": handle.output_mode,
    }
    if handle.exit_code is not None:
        payload["exit_code"] = handle.exit_code
    if handle.halt_reason is not None:
        payload["halt_reason"] = handle.halt_reason
    (handle.run_dir / STATE_FILE).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def read_state(run_dir: Path) -> dict[str, Any] | None:
    state_path = run_dir / STATE_FILE
    if not state_path.is_file():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_meta_task(run_dir: Path) -> str | None:
    """Return ``meta.json:task`` for ``run_dir`` or None.

    Resume uses this to re-supply ``--task`` to the orchestrator
    subprocess; orcho-core's argparse validates the task before the
    ``--resume`` branch is taken, so an empty argv would error out.
    """
    meta_path = run_dir / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    task = meta.get("task")
    return task if isinstance(task, str) and task else None


def read_meta_profile(run_dir: Path) -> str | None:
    """Return ``meta.json:profile`` for ``run_dir`` or None.

    ``Supervisor.resume`` uses this to inherit the original run's
    profile when the caller does not pass an explicit ``profile``
    override. Mirrors :func:`read_meta_task` (silent on missing /
    malformed meta — the absence of a profile field is not an error,
    just falls back to the supervisor's resume-default).
    """
    meta_path = run_dir / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    profile = meta.get("profile")
    return profile if isinstance(profile, str) and profile.strip() else None


# Statuses that mean "the pipeline finished" from cancel's point of view.
#
# Anchored to the pipeline's contract (``meta.json:status``), not to the
# supervisor's internal handle.status, because meta is the authoritative
# completion signal — the supervisor's ``_reap`` updates handle.status
# after ``Popen.wait`` returns, which lags the pipeline's own
# meta-status write by a measurable window. ``cancel`` uses this set to
# avoid sending a SIGTERM into a process that has already exited (or is
# closing) in that window.
#
# ``awaiting_phase_handoff`` is intentionally NOT included: it is
# terminal-LIKE for the subprocess (rc=4) but it is a paused state, not
# a finished one. ``orcho_run_cancel`` on a paused run is a legitimate
# user action (halt the pause); ``orcho_phase_handoff_decide(action=
# "halt")`` is the cleaner path but cancel must not pretend the run is
# already done.
META_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "done",
    "failed",
    "halted",
    "interrupted",
    "orphaned",
})


def meta_status_is_terminal(run_dir: Path) -> bool:
    """Return True iff ``meta.json:status`` reports a finished run.

    Source of truth for "did the pipeline finish" — see
    :data:`META_TERMINAL_STATUSES` for the included set and the
    rationale for excluding ``awaiting_phase_handoff``.

    Missing / malformed meta returns False so callers fall back to
    whatever non-meta check they had (e.g. ``Popen.poll()``); never
    asserts terminality on absent evidence.
    """
    meta_path = run_dir / "meta.json"
    if not meta_path.is_file():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    status = meta.get("status")
    return isinstance(status, str) and status in META_TERMINAL_STATUSES


__all__ = [
    "META_TERMINAL_STATUSES",
    "STATE_FILE",
    "meta_status_is_terminal",
    "now_iso",
    "read_meta_profile",
    "read_meta_task",
    "read_state",
    "write_state",
]
