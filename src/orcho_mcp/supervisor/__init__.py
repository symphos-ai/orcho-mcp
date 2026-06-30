"""orcho_mcp.supervisor — runs supervisor for the execute tools.

Spawns ``pipeline.project_orchestrator`` as a detached subprocess per
run, tracks lifecycle (cancel / reap), persists state so restart-recovery
can detect orphans, and provides the execution surface used by
``orcho_run_start``, ``orcho_run_resume``, and ``orcho_run_cancel``.

Why subprocess (not asyncio task in the MCP server):
  - Pipeline does blocking IO and shells out to Claude Code / Codex CLIs
    that themselves spawn long-lived child processes. Running the pipeline
    in-process inside the asyncio MCP server would block the event loop
    and tangle stdout (which MCP stdio transport reserves for protocol
    frames).
  - Subprocess gives us a real PID, hard cancel via signal, and crash
    isolation. Each run is one Popen, in its own process group
    (``start_new_session=True``) so ``os.killpg`` reaches the whole tree.

State:
  In-memory ``dict[run_id → RunHandle]`` for runs spawned by *this*
  supervisor instance. Each run also writes
  ``<run_dir>/mcp_supervisor.json`` so a restarted supervisor can detect
  orphans by reading the file and probing pid liveness with
  ``os.kill(pid, 0)``. ``meta.json`` stays untouched — it's the
  pipeline's contract.

Package layout (each domain lives in its own submodule):

  ``handle``      ``RunHandle`` dataclass (pure data carrier).
  ``state``      ``mcp_supervisor.json`` IO + meta probes.
  ``paths``      workspace / project path resolution.
  ``process``    pid liveness probe.
  ``spawn``      ``spawn.execute`` (module function).
  ``resume``     ``resume.execute`` (module function).
  ``cancel``     ``cancel.execute`` (module function).
  ``lifecycle``  ``lifecycle.reap`` (module function).
  ``recovery``   ``recovery.recover`` (module function).
  ``manager``    ``RunsSupervisor`` (delegates to operation modules
                 via thin methods; owns in-memory state).

This ``__init__`` exposes ``RunHandle``, ``RunsSupervisor``, and
``get_supervisor`` as the public package surface and owns the
module-level ``_singleton`` so the acceptance fixture's reset
(``orcho_mcp.supervisor._singleton = None``) lands on the right
attribute.
"""
from __future__ import annotations

from orcho_mcp.supervisor.handle import RunHandle
from orcho_mcp.supervisor.manager import RunsSupervisor

# Module-level singleton wired by ``orcho_mcp.server`` at startup.
# MUST live on the package ``__init__`` (not on ``manager``) — the
# acceptance L4 fixture resets it via
# ``orcho_mcp.supervisor._singleton = None`` and that attribute write
# only lands here.
_singleton: RunsSupervisor | None = None


def get_supervisor() -> RunsSupervisor:
    """Return the process-wide supervisor instance, creating it on first call."""
    global _singleton
    if _singleton is None:
        _singleton = RunsSupervisor()
    return _singleton


__all__ = [
    "RunHandle",
    "RunsSupervisor",
    "get_supervisor",
]
