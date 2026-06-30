"""orcho_mcp.event_tail — JSONL tail thread for a run's events.jsonl.

Standalone utility for polling a run's ``events.jsonl`` file, parsing each new
line, and pushing parsed events via a thread-safe callback into the main asyncio
event loop. Current production progress notifications are emitted by
``orcho_mcp.observe.watch``; this module is available for code paths that need a
dedicated file tailer.

Design choices:
  - **Stdlib polling, not watchdog.** events.jsonl is line-buffered + flushed
    on every emit (see core.observability.events.emit), and the file lives
    on the local FS. inotify/FSEvents would be marginally faster but add a
    dep and platform-specific code paths.
  - **Threaded reader, asyncio dispatcher.** Reading file IO blocks; we don't
    want to block the FastMCP event loop. Thread reads, packages each event,
    posts to the event loop via ``loop.call_soon_threadsafe(callback, event)``.
  - **last_seq from disk.** On restart-recovery we re-read ``events.jsonl``
    to find the highest seen seq before tailing — so we don't duplicate
    events that the pipeline already wrote.
  - **EOF on subprocess death.** Caller passes ``stop_predicate`` (e.g.
    ``lambda: popen.poll() is not None``); after one final read pass we exit.
"""
from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TailedEvent:
    """One parsed event from events.jsonl. Mirrors the on-disk shape."""
    seq: int
    ts: str
    kind: str
    phase: str | None
    payload: dict[str, Any]


def _parse_line(line: str) -> TailedEvent | None:
    """Parse one JSONL line into a TailedEvent, or None if malformed/partial.

    Tolerant of partial lines (trailing line without newline during write)
    and any JSON-decode failures.
    """
    line = line.strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None
    try:
        return TailedEvent(
            seq=int(d.get("seq", 0)),
            ts=str(d.get("ts", "")),
            kind=str(d.get("kind", "")),
            phase=d.get("phase"),
            payload=dict(d.get("payload") or {}),
        )
    except (ValueError, TypeError):
        return None


class JsonlTailer:
    """Tail a run's events.jsonl in a background thread.

    Lifecycle:
        tailer = JsonlTailer(run_dir, on_event=callback)
        tailer.start()
        # ... events flow into callback ...
        tailer.stop()        # signal exit
        tailer.join()        # wait for thread to finish
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        on_event: Callable[[TailedEvent], None],
        poll_interval: float = 0.1,
        stop_predicate: Callable[[], bool] | None = None,
        start_seq: int | None = None,
    ):
        """
        Args:
            run_dir: directory containing ``events.jsonl``.
            on_event: called for every new event (in seq order). Will be
                invoked from the tailer thread; if the caller needs to land
                in an asyncio loop, the callback should ``call_soon_threadsafe``
                itself.
            poll_interval: seconds between disk reads at EOF.
            stop_predicate: optional ``() -> bool``; when it returns True,
                tailer does one final drain and exits.
            start_seq: only emit events with seq > start_seq. If None, the
                tailer scans the file once on startup to find max(seq) and
                uses that — replaying nothing. Pass ``0`` to replay from the
                beginning.
        """
        self._run_dir = run_dir
        self._events_path = run_dir / "events.jsonl"
        self._on_event = on_event
        self._poll = poll_interval
        self._stop_predicate = stop_predicate
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Resolve start_seq: explicit value > scan disk > 0 (replay all).
        if start_seq is not None:
            self._last_seq = start_seq
        else:
            self._last_seq = self._scan_max_seq()

    def _scan_max_seq(self) -> int:
        """One-shot scan of events.jsonl for max(seq). Used at startup so a
        re-attach after MCP server restart doesn't replay what's already
        been delivered."""
        if not self._events_path.is_file():
            return 0
        max_seq = 0
        try:
            with self._events_path.open("r", encoding="utf-8") as f:
                for line in f:
                    evt = _parse_line(line)
                    if evt is not None and evt.seq > max_seq:
                        max_seq = evt.seq
        except OSError:
            return 0
        return max_seq

    def start(self) -> None:
        """Start the background thread."""
        if self._thread is not None:
            raise RuntimeError("JsonlTailer already started")
        self._thread = threading.Thread(
            target=self._run,
            name=f"JsonlTailer:{self._run_dir.name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the thread to exit. Use ``join()`` to wait."""
        self._stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the thread to finish."""
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def _run(self) -> None:
        """Thread body: poll the file, dispatch new events, exit on stop."""
        while not self._stop_event.is_set():
            self._drain()
            if self._stop_predicate is not None and self._stop_predicate():
                # One final drain to catch events the pipeline wrote between
                # our last read and its exit.
                self._drain()
                return
            self._stop_event.wait(self._poll)

        # Got explicit stop(); still drain once so callers can rely on
        # "after stop+join, no more events".
        self._drain()

    def _drain(self) -> None:
        """Read all new events with seq > self._last_seq and dispatch them."""
        if not self._events_path.is_file():
            return
        try:
            with self._events_path.open("r", encoding="utf-8") as f:
                for line in f:
                    evt = _parse_line(line)
                    if evt is None or evt.seq <= self._last_seq:
                        continue
                    self._last_seq = evt.seq
                    # Callback failures must not kill the tailer.
                    with contextlib.suppress(Exception):
                        self._on_event(evt)
        except OSError:
            return


__all__ = ["TailedEvent", "JsonlTailer"]
