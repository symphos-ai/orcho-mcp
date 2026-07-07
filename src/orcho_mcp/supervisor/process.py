"""orcho_mcp.supervisor.process — pid liveness probe.

Single-helper module. Kept separate from state / path concerns so the
"does this pid exist?" question has one canonical import home for cancel
and recovery. The probe itself is delegated to
``sdk.run_control.launch.is_pid_alive`` — the same framework-neutral
implementation the SDK launch surface uses — so MCP and SDK agree on
liveness semantics without duplicating the ``os.kill(pid, 0)`` dance.
"""
from __future__ import annotations

from sdk.run_control.launch import is_pid_alive

__all__ = ["is_pid_alive"]
