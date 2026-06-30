"""Advisory MCP workspace state — last observed cursor per run.

Lives at ``<ORCHO_WORKSPACE>/mcp/state.json`` and lets clients reconnect
without replaying from ``since_seq=0`` after a chat/tool/session
restart. **Advisory only**: the canonical truth is the run's
``events.jsonl`` + ``meta.json`` artifacts, exactly as before. Anything
read from this file must be tolerant of:

- the file missing (cold start, new workspace);
- the file being corrupt (partial write recovered from prior crash);
- the workspace directory being read-only (best-effort write skipped).

Design constraints (load-bearing):

- **No daemon, no background polling.** Updates ride on existing read
  paths (``orcho_run_events_summary`` and through it ``orcho_run_watch``).
- **No raw event payloads, no findings, no prompts, no secrets, no env.**
  The schema is deliberately narrow — six string/int slots per run plus
  four envelope timestamps — so the file cannot accidentally become a
  PII trough.
- **No client-side cursor registry.** Per-client tracking belongs to a
  future per-session layer, not the workspace file.
- **No mutation of orcho-core artifacts.** We never write outside
  ``<ORCHO_WORKSPACE>/mcp/``.

Atomicity + concurrency:

- Writes go to a sibling tempfile in the same directory, then
  ``os.replace`` swaps it in (POSIX atomic on the same filesystem;
  Windows ``os.replace`` is also atomic on the same volume).
- A sidecar ``state.json.lock`` is held with an exclusive byte-range
  lock for the whole read-modify-write cycle so two concurrent
  observers cannot clobber each other's last_seq advance. Locking is
  platform-aware (``fcntl.flock`` on POSIX, ``msvcrt.locking`` on
  Windows) — see ``_acquire_lock`` / ``_release_lock``. The lock file
  is intentionally separate from ``state.json`` itself — locking the
  target would block readers on a writer.
- Lock files are not deleted between operations; their lifetime is the
  workspace, not the call.

Schema version 1 is what we write today. Bumping the version is the
contract change for any future shape — readers must tolerate the
version they understand, not fall over on a newer file.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Platform-aware advisory locking. fcntl is POSIX-only — importing it at
# module load on Windows raises ``ModuleNotFoundError`` before
# orcho_mcp can even start its server. We pick the stdlib API that exists
# on the current platform and expose a uniform ``_acquire_lock`` /
# ``_release_lock`` pair below.
if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _acquire_lock(fileno: int) -> None:
        """Acquire an exclusive byte-range lock at offset 0.

        ``msvcrt.locking`` with ``LK_LOCK`` blocks for up to ~10 seconds
        retrying once per second, then raises ``OSError``. The lock
        scope is 1 byte at the current file position — for our empty
        sidecar lock file at position 0 that is enough, and the byte
        does not need to exist on disk.
        """
        msvcrt.locking(fileno, msvcrt.LK_LOCK, 1)

    def _release_lock(fileno: int) -> None:
        """Release the byte-range lock acquired by ``_acquire_lock``."""
        msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _acquire_lock(fileno: int) -> None:
        """POSIX exclusive flock — blocks until granted, auto-released
        on fd close or process death."""
        fcntl.flock(fileno, fcntl.LOCK_EX)

    def _release_lock(fileno: int) -> None:
        """Explicit unlock — paired with the close in the context
        manager so the ordering is obvious to readers."""
        fcntl.flock(fileno, fcntl.LOCK_UN)

logger = logging.getLogger(__name__)

#: Schema version stamped onto every write. Bump on shape changes — the
#: normaliser drops any run whose record does not parse against the
#: current shape, so older readers still produce a valid (possibly
#: empty) state.
_STATE_VERSION = 1

#: Captured once at import time so every state write surfaces when *this*
#: MCP server process started. Useful for diagnosing "did the file
#: outlive the server" scenarios from a single open() of state.json.
_SERVER_STARTED_AT: str = datetime.now(UTC).strftime(
    "%Y-%m-%dT%H:%M:%SZ",
)


@dataclass(frozen=True)
class WorkspaceRunState:
    """Internal frozen view of a single run's last observed state.

    Mirrors ``schemas.WorkspaceRunStateRecord`` but stays inside this
    module so the public wire model can evolve without dragging the
    internal helpers along.
    """

    run_id: str
    last_seq: int
    last_status: str | None
    last_phase: str | None
    last_summary_at: str


@dataclass(frozen=True)
class WorkspaceMcpState:
    """Internal frozen view of the full state file. Same caveats as
    ``WorkspaceRunState``."""

    version: int
    workspace_dir: str
    server_started_at: str
    updated_at: str
    runs: dict[str, WorkspaceRunState]


# ── path resolution ──────────────────────────────────────────────────────────


def state_path(workspace_dir: Path) -> Path:
    """Return the canonical state file path for ``workspace_dir``.

    Does not create the parent directory — that happens lazily on first
    write so a read-only workspace stays read-only.
    """
    return workspace_dir / "mcp" / "state.json"


def _lock_path(workspace_dir: Path) -> Path:
    return workspace_dir / "mcp" / "state.json.lock"


# ── time helpers ─────────────────────────────────────────────────────────────


def _utc_now() -> str:
    """RFC 3339 / ISO 8601 UTC timestamp with seconds precision and ``Z``.

    Fixed format so the file diffs stay stable across writes and the
    string sorts lexicographically.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _server_started_at() -> str:
    """Module-level constant — captured at import. Exposed as a function
    so tests can monkeypatch in deterministic values."""
    return _SERVER_STARTED_AT


# ── locking + atomic write ───────────────────────────────────────────────────


@contextlib.contextmanager
def _locked_state_file(workspace_dir: Path) -> Iterator[None]:
    """Hold an exclusive lock on the workspace's state.json.lock for
    the duration of a read-modify-write cycle.

    Locking is platform-aware (see ``_acquire_lock`` /
    ``_release_lock``). On POSIX, ``fcntl.flock`` auto-releases on fd
    close or process death — so a crash never leaves a stale lock. On
    Windows, ``msvcrt.locking`` releases when the fd closes; the
    closing ``with`` block guarantees that.

    Callers must keep this context open for the entire read-modify-
    write cycle. The lock file itself is created on demand and persists
    between calls.
    """
    lock_path = _lock_path(workspace_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # ``a+`` so we never truncate; we never read from the lock file
    # itself, only lock its descriptor.
    with lock_path.open("a+") as fh:
        _acquire_lock(fh.fileno())
        try:
            yield
        finally:
            # Lock auto-releases on close; explicit unlock keeps the
            # ordering obvious to readers and is required by
            # ``msvcrt.locking`` semantics on Windows.
            with contextlib.suppress(OSError):
                _release_lock(fh.fileno())


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as JSON to ``path`` atomically.

    Strategy: tempfile in the same directory + ``os.replace``. Same
    filesystem guarantee means the swap is atomic on POSIX, and the
    tempfile inherits the directory's mount so we cannot accidentally
    cross filesystems and degrade to copy-then-rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp gives us a raw fd + a unique pathname; we wrap the fd in
    # the standard ``with`` so the file handle closes promptly, then
    # ``os.replace`` swaps the on-disk file into place. Using mkstemp
    # over NamedTemporaryFile keeps the lifecycle explicit (no
    # delete-on-close trap) and avoids the SIM115 warning around
    # ``delete=False`` usage. The caller holds the workspace lock, so
    # the tempfile name cannot collide with a concurrent writer.
    fd, tmp_name = tempfile.mkstemp(
        prefix=".state.", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup; the lock guarantees no one else is racing
        # us on this exact tempfile name.
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


# ── normalisation ────────────────────────────────────────────────────────────


def _empty_state(workspace_dir: Path) -> dict[str, Any]:
    """Return a fresh empty state envelope for ``workspace_dir``.

    Shared between the cold-start path and the corrupt-file recovery
    path so both produce the same shape — tests pin this.
    """
    return {
        "version": _STATE_VERSION,
        "workspace_dir": str(workspace_dir),
        "server_started_at": _server_started_at(),
        "updated_at": _utc_now(),
        "runs": {},
    }


def _normalise_run_record(raw: Any) -> dict[str, Any] | None:
    """Project one raw run entry onto the strict wire shape, or drop it.

    Defensive: a single malformed run record must not poison the rest of
    the file. Anything that fails the shape check returns ``None`` and
    is silently dropped on read — the run will be re-recorded on the
    next observation if it still exists.
    """
    if not isinstance(raw, dict):
        return None
    run_id = raw.get("run_id")
    last_seq = raw.get("last_seq")
    if not isinstance(run_id, str) or not run_id:
        return None
    if not isinstance(last_seq, int) or last_seq < 0:
        return None
    last_status = raw.get("last_status")
    if last_status is not None and not isinstance(last_status, str):
        last_status = None
    last_phase = raw.get("last_phase")
    if last_phase is not None and not isinstance(last_phase, str):
        last_phase = None
    last_summary_at = raw.get("last_summary_at")
    if not isinstance(last_summary_at, str) or not last_summary_at:
        return None
    return {
        "run_id": run_id,
        "last_seq": last_seq,
        "last_status": last_status,
        "last_phase": last_phase,
        "last_summary_at": last_summary_at,
    }


def _normalise_state(raw: object, workspace_dir: Path) -> dict[str, Any]:
    """Project arbitrary disk content onto the strict wire shape.

    Corrupt / unknown-shaped files come out as a fresh empty state.
    Known-shape files with a few malformed run records lose just those
    records — never the whole file. This is the read-side equivalent of
    Postel's law: be liberal with what arrived, strict with what we
    return.
    """
    if not isinstance(raw, dict):
        return _empty_state(workspace_dir)
    version = raw.get("version")
    if version != _STATE_VERSION:
        # Future-proofing slot: when version bumps, this branch decides
        # whether to migrate or discard. Today, anything off-version is
        # treated as cold-start.
        return _empty_state(workspace_dir)
    runs_raw = raw.get("runs")
    runs: dict[str, dict[str, Any]] = {}
    if isinstance(runs_raw, dict):
        for key, value in runs_raw.items():
            if not isinstance(key, str):
                continue
            normalised = _normalise_run_record(value)
            if normalised is None:
                continue
            # Trust the run record's own ``run_id`` if it agrees with the
            # key; otherwise prefer the key (it is the canonical id at
            # the dict-of-runs layer).
            if normalised["run_id"] != key:
                normalised = {**normalised, "run_id": key}
            runs[key] = normalised
    server_started_at = raw.get("server_started_at")
    if not isinstance(server_started_at, str) or not server_started_at:
        server_started_at = _server_started_at()
    updated_at = raw.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        updated_at = _utc_now()
    # ``workspace_dir`` is always overridden with the resolved path.
    # The state file lives at ``<workspace_dir>/mcp/state.json``, so by
    # definition the authoritative value is whatever the resolver gave
    # us. Trusting the on-disk value would surface stale paths when a
    # state file has been copied or the workspace has moved on disk —
    # confusing reconnect / debug UX with no upside.
    return {
        "version": _STATE_VERSION,
        "workspace_dir": str(workspace_dir),
        "server_started_at": server_started_at,
        "updated_at": updated_at,
        "runs": runs,
    }


# ── public read/update API ───────────────────────────────────────────────────


def read_workspace_state(workspace_dir: Path) -> dict[str, Any]:
    """Return the current state, or a fresh empty state if missing/corrupt.

    Does not acquire the lock — readers tolerate seeing an intermediate
    state, and acquiring the exclusive lock on every read would
    bottleneck concurrent observers behind a single writer.

    Caller gets a JSON-serialisable ``dict``; the public ``orcho_workspace_state``
    tool projects this onto ``WorkspaceMcpStateResult``.
    """
    path = state_path(workspace_dir)
    if not path.is_file():
        return _empty_state(workspace_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.debug(
            "advisory MCP state at %s is unreadable/corrupt; "
            "returning empty state",
            path,
            exc_info=True,
        )
        return _empty_state(workspace_dir)
    return _normalise_state(raw, workspace_dir)


def update_run_state(
    workspace_dir: Path,
    *,
    run_id: str,
    last_seq: int,
    last_status: str | None,
    last_phase: str | None,
) -> dict[str, Any]:
    """Record an observation for ``run_id``.

    Semantics:

    - ``last_seq`` is monotonic per run. If the existing record has a
      higher seq, the new observation is dropped on the seq dimension —
      but ``last_status`` / ``last_phase`` / ``last_summary_at`` still
      refresh, because the higher seq is consistent with the new
      status (the cursor just hasn't moved).
    - If the existing record has the same or a lower seq, the run
      record is replaced wholesale.
    - Missing run record → inserted.

    Inputs must be sane: ``run_id`` non-empty, ``last_seq >= 0``,
    timestamps are added by this function, not the caller, so race-y
    callers cannot poison the file with bogus times.
    """
    if not isinstance(run_id, str) or not run_id:
        # Caller bug; surface loudly. The wiring in ``tools.py`` swallows
        # exceptions from this function, so a programmer error here
        # degrades to a silent skip, not a tool failure.
        raise ValueError("update_run_state: run_id must be a non-empty str")
    if not isinstance(last_seq, int) or last_seq < 0:
        raise ValueError(
            f"update_run_state: last_seq must be >= 0, got {last_seq!r}",
        )

    with _locked_state_file(workspace_dir):
        state = read_workspace_state(workspace_dir)
        existing = state["runs"].get(run_id)
        now = _utc_now()
        if existing is not None and existing["last_seq"] > last_seq:
            # Cursor cannot move backwards. Refresh the timestamp +
            # status/phase only — these are derived from a later wall
            # clock and the cursor may legitimately lag (e.g. status
            # ticking forward without new events).
            state["runs"][run_id] = {
                "run_id": run_id,
                "last_seq": existing["last_seq"],
                "last_status": last_status,
                "last_phase": last_phase,
                "last_summary_at": now,
            }
        else:
            state["runs"][run_id] = {
                "run_id": run_id,
                "last_seq": last_seq,
                "last_status": last_status,
                "last_phase": last_phase,
                "last_summary_at": now,
            }
        state["updated_at"] = now
        # Keep workspace_dir + server_started_at honest on each write —
        # if the file was created by a different process / instance, our
        # next write claims it.
        state["workspace_dir"] = str(workspace_dir)
        state["server_started_at"] = _server_started_at()
        _atomic_write_json(state_path(workspace_dir), state)
        return state


def clear_run_state(
    workspace_dir: Path, run_id: str,
) -> dict[str, Any]:
    """Drop one run from the state file, leaving everything else intact.

    Not currently wired to a tool; exposed so future cleanup
    flows (e.g. an admin "forget this run" path) can stay consistent
    with the read/update API. Missing run is a no-op.
    """
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("clear_run_state: run_id must be a non-empty str")
    with _locked_state_file(workspace_dir):
        state = read_workspace_state(workspace_dir)
        if run_id in state["runs"]:
            del state["runs"][run_id]
            state["updated_at"] = _utc_now()
            state["workspace_dir"] = str(workspace_dir)
            state["server_started_at"] = _server_started_at()
            _atomic_write_json(state_path(workspace_dir), state)
        return state


__all__ = [
    "WorkspaceMcpState",
    "WorkspaceRunState",
    "clear_run_state",
    "read_workspace_state",
    "state_path",
    "update_run_state",
]
