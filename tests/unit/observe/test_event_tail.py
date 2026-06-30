"""Unit tests for event_tail.

Tests the JsonlTailer thread against synthetic events.jsonl files.
No real pipeline subprocess; we control writes from the test thread.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from orcho_mcp.event_tail import JsonlTailer, TailedEvent, _parse_line


def _ev(seq: int, **kw) -> dict:
    return {
        "seq": seq,
        "ts": f"2026-05-06T12:00:{seq:02d}.000",
        "kind": kw.get("kind", "phase.start"),
        "phase": kw.get("phase", "plan"),
        "payload": kw.get("payload", {}),
    }


def _write_events(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def _append_events(path: Path, events: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
        f.flush()


def test_parse_valid_line():
    line = json.dumps({"seq": 5, "ts": "t", "kind": "k", "phase": "p", "payload": {"a": 1}})
    evt = _parse_line(line)
    assert evt is not None
    assert evt.seq == 5
    assert evt.kind == "k"
    assert evt.payload == {"a": 1}


def test_parse_blank_line_returns_none():
    assert _parse_line("") is None
    assert _parse_line("   \n") is None


def test_parse_partial_json_returns_none():
    assert _parse_line('{"seq": 1, "ts": "t', ) is None


def test_tailer_replays_from_zero(tmp_path):
    """start_seq=0 replays everything."""
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_ev(1), _ev(2), _ev(3)])

    received: list[TailedEvent] = []
    tailer = JsonlTailer(
        tmp_path,
        on_event=lambda e: received.append(e),
        poll_interval=0.05,
        start_seq=0,
    )
    tailer.start()
    time.sleep(0.15)  # let it drain
    tailer.stop()
    tailer.join(timeout=1.0)

    assert [e.seq for e in received] == [1, 2, 3]


def test_tailer_skips_already_seen(tmp_path):
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_ev(1), _ev(2), _ev(3)])

    received: list[TailedEvent] = []
    tailer = JsonlTailer(
        tmp_path,
        on_event=lambda e: received.append(e),
        poll_interval=0.05,
        start_seq=2,
    )
    tailer.start()
    time.sleep(0.15)
    tailer.stop()
    tailer.join(timeout=1.0)

    assert [e.seq for e in received] == [3]


def test_tailer_default_start_seq_is_max_existing(tmp_path):
    """Without start_seq, tailer scans file for max(seq) and replays nothing."""
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_ev(1), _ev(2)])

    received: list[TailedEvent] = []
    tailer = JsonlTailer(tmp_path, on_event=lambda e: received.append(e), poll_interval=0.05)
    tailer.start()
    time.sleep(0.15)
    tailer.stop()
    tailer.join(timeout=1.0)

    assert received == []
    assert tailer.last_seq == 2


def test_tailer_picks_up_appended_events(tmp_path):
    """Live append: tailer keeps polling and dispatches new events."""
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_ev(1)])

    received: list[TailedEvent] = []
    lock = threading.Lock()

    def callback(e):
        with lock:
            received.append(e)

    tailer = JsonlTailer(tmp_path, on_event=callback, poll_interval=0.05, start_seq=0)
    tailer.start()
    time.sleep(0.1)  # let initial event land

    _append_events(events_path, [_ev(2), _ev(3)])
    time.sleep(0.2)  # let tailer pick up the appends

    tailer.stop()
    tailer.join(timeout=1.0)

    with lock:
        seqs = [e.seq for e in received]
    assert seqs == [1, 2, 3]


def test_tailer_stop_predicate_exits_thread(tmp_path):
    """stop_predicate True triggers final drain + exit."""
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_ev(1)])

    flag = threading.Event()
    received: list[TailedEvent] = []
    tailer = JsonlTailer(
        tmp_path,
        on_event=lambda e: received.append(e),
        poll_interval=0.05,
        stop_predicate=flag.is_set,
        start_seq=0,
    )
    tailer.start()
    time.sleep(0.1)
    flag.set()
    tailer.join(timeout=1.0)

    assert [e.seq for e in received] == [1]


def test_tailer_handles_missing_file(tmp_path):
    """No events.jsonl yet — tailer should not crash."""
    received: list[TailedEvent] = []
    tailer = JsonlTailer(tmp_path, on_event=lambda e: received.append(e), poll_interval=0.05)
    tailer.start()
    time.sleep(0.1)

    # Now create the file — tailer should pick events up
    _write_events(tmp_path / "events.jsonl", [_ev(1)])
    time.sleep(0.15)

    tailer.stop()
    tailer.join(timeout=1.0)

    assert [e.seq for e in received] == [1]


def test_tailer_callback_failures_dont_break_thread(tmp_path):
    events_path = tmp_path / "events.jsonl"
    _write_events(events_path, [_ev(1), _ev(2)])

    received: list[TailedEvent] = []

    def failing_callback(evt):
        if evt.seq == 1:
            raise RuntimeError("boom")
        received.append(evt)

    tailer = JsonlTailer(tmp_path, on_event=failing_callback, poll_interval=0.05, start_seq=0)
    tailer.start()
    time.sleep(0.15)
    tailer.stop()
    tailer.join(timeout=1.0)

    # Event 1 raised, event 2 still delivered.
    assert [e.seq for e in received] == [2]


def test_tailer_double_start_raises(tmp_path):
    tailer = JsonlTailer(tmp_path, on_event=lambda _: None, poll_interval=0.05)
    tailer.start()
    try:
        with pytest.raises(RuntimeError):
            tailer.start()
    finally:
        tailer.stop()
        tailer.join(timeout=1.0)
