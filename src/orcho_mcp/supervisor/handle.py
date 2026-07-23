"""orcho_mcp.supervisor.handle — in-memory ``RunHandle`` dataclass.

Pure data carrier for a tracked run. No IO, no subprocess logic — the
typed bag every supervisor mixin shares state through.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from subprocess import Popen


@dataclass
class RunHandle:
    """In-memory record for a tracked run.

    For runs we spawned ourselves, ``popen`` is the live ``Popen`` we use
    for ``wait()``-based reaping. For runs picked up via restart-recovery,
    ``popen`` is ``None`` — we can still cancel via ``os.kill`` on the pid.

    ``mock`` records whether the original spawn was a ``--mock`` run.
    Resume must thread the same flag back into the resumed subprocess
    argv, otherwise a paused mock run resumed without ``--mock`` will
    fall through to the real provider CLI on its first review/build call
    and fail outside the test environment.

    ``output_mode`` records the transcript mode from the original spawn so
    resume continues with the same stdout/trace behaviour.
    """
    run_id: str
    pid: int
    pgid: int
    run_dir: Path
    project_dir: str
    command: list[str]
    started_at: str
    status: str = "running"
    progress_token: str | None = None
    last_seq: int = 0
    exit_code: int | None = None
    halt_reason: str | None = None
    mock: bool = False
    output_mode: str = "summary"
    popen: Popen | None = field(default=None, repr=False)


__all__ = ["RunHandle"]
