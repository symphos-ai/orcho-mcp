"""SDK-backed projection of the active verification command."""
from __future__ import annotations

from dataclasses import asdict

from sdk.gate_progress import read_active_gate_progress

from orcho_mcp.schemas.observe import RunLiveGateProgress
from orcho_mcp.services.errors import map_sdk_errors


def project_active_gate(run_id: str) -> RunLiveGateProgress | None:
    """Read durable progress; keep transport tails bounded defensively."""
    with map_sdk_errors(run_id):
        snapshot = read_active_gate_progress(run_id, cwd=None)
    if snapshot is None:
        return None
    data = asdict(snapshot)
    for stream in ("stdout_tail", "stderr_tail"):
        data[stream] = data[stream][-2000:]
    return RunLiveGateProgress(**data)
