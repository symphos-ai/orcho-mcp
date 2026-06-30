"""orcho_mcp.services.run_events — SDK-backed event stream reads."""
from __future__ import annotations

from sdk import (
    NoWorkspace as _SDKNoWorkspace,
    RunEvent,
    RunNotFound as _SDKRunNotFound,
)
from sdk.run_control import read_run_events as _rc_read

from orcho_mcp.errors import RunNotFoundError, WorkspaceNotResolvedError


def read_run_events(run_id: str) -> tuple[RunEvent, ...]:
    """Return every event recorded for ``run_id`` in seq order.

    Routed through ``sdk.run_control.read_run_events`` (the run-control
    read model) with ``cwd=None`` so resolution comes solely from the
    ambient workspace / runs_dir — no walk-up. Output and the
    ``find_run`` error sources are identical to the previous direct
    ``sdk.list_events`` call.
    """
    try:
        return _rc_read(run_id, cwd=None)
    except _SDKRunNotFound as e:
        raise RunNotFoundError(f"run not found: {run_id}") from e
    except _SDKNoWorkspace as e:
        raise WorkspaceNotResolvedError(str(e)) from e


__all__ = ["read_run_events"]
