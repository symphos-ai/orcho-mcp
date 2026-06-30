"""orcho_mcp.supervisor.lifecycle — ``reap`` post-mortem.

Blocks until the spawned subprocess exits, maps the rc to a status,
and stamps a ``halt_reason`` on abnormal exits (signal-killed,
abnormal exit code) so the wire adapter can surface a reason even
when SIGKILL bypassed the pipeline's own atexit hook. Appends a
synthetic ``run.supervisor_reaped`` event for diagnostics when the
pipeline didn't already emit ``run.end``.

Composed into ``RunsSupervisor`` via the ``_reap`` delegation method
in ``manager.py``; the method is kept private (underscore prefix)
because tests in ``tests/unit/supervisor/test_lifecycle.py`` call
``sup._reap(handle)`` directly. Spawn / resume schedule reap via
``asyncio.create_task(sup._reap(handle))`` — that call site survives
the migration because ``_reap`` stays as a delegation method.
"""
from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import TYPE_CHECKING

from core.observability.events import append_event

from orcho_mcp.supervisor.handle import RunHandle
from orcho_mcp.supervisor.state import write_state

if TYPE_CHECKING:
    from orcho_mcp.supervisor.manager import RunsSupervisor


async def reap(sup: RunsSupervisor, handle: RunHandle) -> None:
    """Block until the subprocess exits, then update state.

    Maps exit codes to status:
      - rc=0 → done
      - rc=4 → awaiting_phase_handoff (pipeline paused on a
        phase's declared handoff policy; resolve via
        ``orcho_phase_handoff_decide`` + ``orcho_run_resume``)
      - other → failed

    Also stamps ``halt_reason`` on abnormal exits so the wire
    adapter can surface a reason even when SIGKILL bypassed the
    pipeline's atexit hook:
      - rc<0 (signal-killed) → ``signal:<NAME>`` (e.g.
        ``signal:SIGKILL``). Falls back to ``signal:<-rc>`` when
        the signal number doesn't map to a known name.
      - rc>0 and rc!=4 → ``abnormal_exit:<rc>`` — the pipeline
        crashed without writing ``meta.halt_reason`` itself.
      - rc=0 or rc=4 → no ``halt_reason`` (success / pause).

    ``meta.json`` stays pipeline-owned. The wire adapter
    (``orcho_run_status``) is responsible for merging supervisor
    truth when meta.json's status / halt_reason is missing
    (pipeline killed before it could update / atexit bypassed by
    SIGKILL / pipeline killed before initial meta.json write).

    The ``sup`` argument is accepted for signature symmetry with the
    other operation modules but not consumed: reap mutates the handle
    directly (status / exit_code / halt_reason) and flushes through
    ``write_state``; no supervisor-instance state is touched.
    """
    del sup  # signature symmetry; handle mutation only
    if handle.popen is None:
        return
    loop = asyncio.get_running_loop()
    rc = await loop.run_in_executor(None, handle.popen.wait)

    handle.exit_code = rc
    if rc == 0:
        handle.status = "done"
    elif rc == 4:
        handle.status = "awaiting_phase_handoff"
    else:
        handle.status = "failed"

    if rc < 0:
        try:
            signame = signal.Signals(-rc).name
        except (ValueError, AttributeError):
            signame = str(-rc)
        handle.halt_reason = f"signal:{signame}"
    elif rc != 0 and rc != 4:
        handle.halt_reason = f"abnormal_exit:{rc}"

    write_state(handle)

    # If the pipeline didn't emit run.end (it does on normal completion,
    # but a crashed pipeline might not), append a synthetic one. We can't
    # cheaply tell whether it did without scanning events.jsonl; for v1
    # we always append a supervisor-side marker that distinguishes from
    # the pipeline's own run.end via ``kind`` choice.
    if rc != 0:
        # Append failures are non-fatal; the state file is the
        # authoritative record for supervisor consumers.
        with contextlib.suppress(Exception):
            append_event(
                handle.run_dir,
                "run.supervisor_reaped",
                {"exit_code": rc, "status": handle.status},
            )


__all__ = ["reap"]
