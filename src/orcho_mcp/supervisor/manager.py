"""orcho_mcp.supervisor.manager — ``RunsSupervisor`` state + delegations.

Owns the in-memory state (``_runs``, ``_project_locks``, ``_max_runs``)
and exposes operations as thin delegation methods that forward to the
matching module function in ``spawn`` / ``resume`` / ``cancel`` /
``lifecycle`` / ``recovery``. Operation modules read state via
``sup.<attr>`` directly; this class owns the storage, the operations
own the behaviour.

Each delegation method preserves the exact signature of the operation
function (no ``**kw`` forwarding) so
``inspect.signature(RunsSupervisor.spawn)`` and friends stay
bit-identical to the pre-composition shape. The signature contract is
tested by ``test_runs_supervisor_signatures_stable`` in
``tests/unit/architecture/test_supervisor_boundary.py``.

Module-level singleton lives in :mod:`orcho_mcp.supervisor` (package
``__init__``), not here — the acceptance fixture resets it via
``orcho_mcp.supervisor._singleton = None`` and that assignment has to
land on the package namespace to actually take effect.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime

import orcho_mcp.supervisor.cancel as cancel_ops
import orcho_mcp.supervisor.lifecycle as lifecycle_ops
import orcho_mcp.supervisor.recovery as recovery_ops
import orcho_mcp.supervisor.resume as resume_ops
import orcho_mcp.supervisor.spawn as spawn_ops
from orcho_mcp.supervisor.handle import RunHandle


class RunsSupervisor:
    """Single supervisor instance owned by the FastMCP server.

    Spawns / tracks / cancels / reaps pipeline subprocesses. Recovery on
    restart scans the runs directory for stale ``mcp_supervisor.json`` files
    and either re-attaches (live pid) or marks orphaned (dead pid).
    """

    def __init__(self, max_runs: int | None = None):
        # Runs we own this lifetime. Re-attached orphans don't enter here
        # because we have no Popen handle for them.
        self._runs: dict[str, RunHandle] = {}
        self._project_locks: dict[str, asyncio.Lock] = {}
        env_max = os.environ.get("ORCHO_MCP_MAX_RUNS", "").strip()
        if env_max:
            try:
                self._max_runs = int(env_max)
            except ValueError:
                self._max_runs = max_runs if max_runs is not None else 4
        else:
            self._max_runs = max_runs if max_runs is not None else 4

    # ── id minting ──────────────────────────────────────────────────────────

    @staticmethod
    def mint_run_id() -> str:
        """``YYYYMMDD_HHMMSS_xxxxxx`` — ts + 6 hex chars for collision safety."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:6]
        return f"{ts}_{token}"

    # ── status / introspection ──────────────────────────────────────────────

    def get(self, run_id: str) -> RunHandle | None:
        """Return the in-memory handle for a run we own, or None."""
        return self._runs.get(run_id)

    def list_active(self) -> list[RunHandle]:
        """Return handles for runs still in ``running`` status."""
        return [h for h in self._runs.values() if h.status == "running"]

    # ── operations (composition delegation) ─────────────────────────────────

    async def spawn(
        self,
        *,
        task: str | None = None,
        task_file: str | None = None,
        project_dir: str,
        profile: str = "feature",
        mock: bool = False,
        max_rounds: int | None = None,
        mock_validate_plan_reject: int = 0,
        output_mode: str = "summary",
        session_mode: str = "auto",
        progress_token: str | None = None,
        attach: list[str] | None = None,
        attach_text: list[str] | None = None,
        attach_image: list[str] | None = None,
        attach_binary: list[str] | None = None,
        from_run_plan: str | None = None,
    ) -> RunHandle:
        """Delegate to :func:`orcho_mcp.supervisor.spawn.execute`.

        Signature copied verbatim from the operation function so
        ``inspect.signature(RunsSupervisor.spawn)`` stays bit-identical
        to the pre-migration shape — it is part of the public contract.
        """
        return await spawn_ops.execute(
            self,
            task=task,
            task_file=task_file,
            project_dir=project_dir,
            profile=profile,
            mock=mock,
            max_rounds=max_rounds,
            mock_validate_plan_reject=mock_validate_plan_reject,
            output_mode=output_mode,
            session_mode=session_mode,
            progress_token=progress_token,
            attach=attach,
            attach_text=attach_text,
            attach_image=attach_image,
            attach_binary=attach_binary,
            from_run_plan=from_run_plan,
        )

    async def resume(self, run_id: str, *, profile: str | None = None) -> RunHandle:
        """Delegate to :func:`orcho_mcp.supervisor.resume.execute`."""
        return await resume_ops.execute(self, run_id, profile=profile)

    async def cancel(self, run_id: str, mode: str = "graceful") -> dict[str, str]:
        """Delegate to :func:`orcho_mcp.supervisor.cancel.execute`."""
        return await cancel_ops.execute(self, run_id, mode)

    async def _reap(self, handle: RunHandle) -> None:
        """Delegate to :func:`orcho_mcp.supervisor.lifecycle.reap`.

        Kept as a method (private) because three tests in
        ``tests/unit/supervisor/test_lifecycle.py`` call
        ``sup._reap(handle)`` directly. Also called by spawn / resume
        via ``asyncio.create_task(sup._reap(handle))``.
        """
        return await lifecycle_ops.reap(self, handle)

    def recover(self) -> list[str]:
        """Delegate to :func:`orcho_mcp.supervisor.recovery.recover`."""
        return recovery_ops.recover(self)


__all__ = ["RunsSupervisor"]
