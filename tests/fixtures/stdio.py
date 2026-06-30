"""Shared L3 stdio protocol plumbing for ``integration/protocol/`` tests.

Owns subprocess spawn, ``stdio_client`` + ``ClientSession`` setup, and
the ``initialize()`` handshake. No domain assertions — just transport
boilerplate so ``tests/integration/protocol/test_*.py`` read as "call
tool, assert result" instead of fighting subprocess plumbing.

``initialized_stdio_session`` is the entire public surface here.
Convenience helpers (progressToken capture, structured call-tool
wrappers, JSON-decode shims, etc.) should be added only after a second
non-trivial consumer appears. The point of this module is to deduplicate
transport plumbing, not to invent a new test DSL.
"""
from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import InitializeResult


def _build_server_params(workspace_dir: Path | None = None) -> StdioServerParameters:
    """``python -m orcho_mcp`` with an optional pinned ``ORCHO_WORKSPACE``.

    Uses ``-m orcho_mcp`` (the package's ``__main__.py`` entry) rather
    than ``-m orcho_mcp.server`` — the latter would dual-import
    ``server.py`` as both ``__main__`` and ``orcho_mcp.server``,
    splitting the FastMCP instance and silently dropping registered
    handlers from the running loop.

    The subprocess ``PYTHONPATH`` is pinned to THIS checkout's ``src`` (and
    repo root) so the spawned server is the source under test, not a stale
    editable / site-packages install. Without this, an in-worktree run would
    resolve ``orcho_mcp`` to the canonical-repo editable install and silently
    test the wrong MCP surface (missing checkout-only tools). Mirrors
    pyproject's ``pythonpath = ["src", "."]`` and ``tools/dump_mcp_schema.py``.
    """
    env = {**os.environ}
    _repo_root = Path(__file__).resolve().parents[2]
    _checkout_paths = [str(_repo_root / "src"), str(_repo_root)]
    _prior_pythonpath = env.get("PYTHONPATH", "")
    if _prior_pythonpath:
        _checkout_paths.append(_prior_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(_checkout_paths)
    if workspace_dir is not None:
        env["ORCHO_WORKSPACE"] = str(workspace_dir)
        # ``runspace_dir()`` prefers ``$ORCHO_RUNSPACE`` over
        # ``$ORCHO_WORKSPACE/runspace``. When the suite runs inside an
        # ambient Orcho run, that override would point the spawned
        # server at the real runspace and the synthetic run written
        # under the pinned workspace would never be found (watch returns
        # an empty payload). Drop it so resolution follows the pinned
        # workspace deterministically.
        env.pop("ORCHO_RUNSPACE", None)
    return StdioServerParameters(
        command=sys.executable, args=["-m", "orcho_mcp"], env=env,
    )


@asynccontextmanager
async def initialized_stdio_session(
    workspace_dir: Path | None = None,
) -> AsyncIterator[tuple[ClientSession, InitializeResult]]:
    """Spawn the MCP server subprocess, open a stdio session, run ``initialize()``.

    Yields ``(session, init_result)`` so handshake tests can still pin
    ``init_result.serverInfo`` while ``call_tool`` tests can ignore the
    init result entirely. Exiting the context tears the subprocess down.

    Tests pass the synthetic workspace path explicitly when they need
    ``$ORCHO_WORKSPACE`` pinned for run discovery; pass ``None`` for
    handshake-only tests that just probe catalog presence.
    """
    params = _build_server_params(workspace_dir)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init_result = await session.initialize()
        yield session, init_result


__all__ = ["initialized_stdio_session"]
