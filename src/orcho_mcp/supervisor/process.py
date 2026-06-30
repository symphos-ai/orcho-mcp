"""orcho_mcp.supervisor.process — pid liveness probe.

Single-helper module. Kept separate from state/path concerns so the
"does this pid exist?" question has one canonical home; cancel and
recovery both consult it.
"""
from __future__ import annotations

import os


def is_pid_alive(pid: int) -> bool:
    """Return True if ``pid`` is alive (or exists with different uid)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists but belongs to another user — treat as alive.
        return True


__all__ = ["is_pid_alive"]
