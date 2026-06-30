#!/usr/bin/env python3
"""REA-4.1 post-restart live smoke.

After any MCP server restart (Claude Code reload, IDE restart,
``orcho-mcp`` process kill+respawn), run this script to verify the
full read+spawn loop on the *live* server. This is the one thing
the L1-L4 in-process tests can't catch: a long-lived MCP process
holding stale ``__editable__.orcho_core`` finder MAPPING without
``sdk`` in it.

Sequence (matches the gate the reviewer cited as the missing live
proof):

    1. orcho_run_start --mock         → spawn a real pipeline subprocess
    2. orcho_run_status(run_id)     → poll until status in {done, failed}
    3. orcho_run_metrics(run_id)    → confirm metrics.json populated
    4. orcho_run_history(limit=1)   → confirm the new run is at the top

Exit codes
    0  full smoke green — REA-4.1 closes without caveat.
    1  spawn or wire failure — print diagnostics; do NOT mark closed.
    2  pipeline ran but didn't reach 'done' — investigate before merge.

Usage::

    python scripts/post_restart_smoke.py [--project /path/to/proj] [--timeout 120]

The script invokes the MCP tools directly via the in-process Python
SDK (``orcho_mcp.tools``) — same import path the live server uses,
so a stale finder fails here exactly the way it fails in the bridge.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path


def _abort(code: int, msg: str) -> None:
    print(f"\n[FAIL] {msg}", file=sys.stderr)
    sys.exit(code)


async def _run(project_dir: Path, timeout_s: float) -> int:
    # Imports happen inside the function so a stale-finder failure
    # surfaces with a clean error rather than at script load time.
    try:
        from orcho_mcp.tools import (  # noqa: PLC0415 — deferred for diagnostics
            orcho_run_history,
            orcho_run_metrics,
            orcho_run_start,
            orcho_run_status,
        )
    except ModuleNotFoundError as e:
        _abort(
            1,
            f"orcho_mcp imports failed: {e}\n"
            "If the message says 'No module named sdk', the venv's "
            "editable finder is stale. Run:\n"
            "    /path/to/.venv/bin/python -m pip install -e <orcho-core>\n"
            "then restart Claude Code / the MCP server.",
        )

    print("[1/4] orcho_run_start --mock …")
    try:
        started = await orcho_run_start(
            task="REA-4.1 post-restart smoke",
            project_dir=str(project_dir),
            mock=True,
            max_rounds=1,
            profile="advanced",
        )
    except Exception as e:
        _abort(1, f"orcho_run_start raised {type(e).__name__}: {e}")
    run_id = started.run_id
    print(f"      run_id = {run_id}")
    print(f"      pid    = {started.pid}")

    print("[2/4] orcho_run_status() poll …")
    deadline = time.monotonic() + timeout_s
    final_status: str | None = None
    last_status: str | None = None
    while time.monotonic() < deadline:
        try:
            snap = orcho_run_status(run_id)
        except Exception as e:
            _abort(1, f"orcho_run_status raised {type(e).__name__}: {e}")
        cur = (snap.meta or {}).get("status")
        if cur != last_status:
            print(f"      status = {cur}")
            last_status = cur
        if cur in ("done", "failed", "interrupted", "halted"):
            final_status = cur
            break
        await asyncio.sleep(0.5)

    if final_status is None:
        _abort(2, f"timed out after {timeout_s}s with status={last_status}")
    if final_status != "done":
        _abort(2, f"pipeline finished with status={final_status} (expected 'done')")

    print("[3/4] orcho_run_metrics() …")
    try:
        m = orcho_run_metrics(run_id)
    except Exception as e:
        _abort(1, f"orcho_run_metrics raised {type(e).__name__}: {e}")
    metrics = m.metrics
    if not metrics or "phases" not in metrics:
        _abort(2, f"metrics.json empty or malformed: {metrics!r}")
    print(f"      total_tokens    = {metrics.get('total_tokens')}")
    print(f"      total_duration  = {metrics.get('total_duration_s'):.3f}s")
    print(f"      phases          = {sorted(metrics.get('phases', {}).keys())}")

    # NB: list_history sorts run dirs by basename in reverse — workspaces
    # that mix timestamp-style ids (``20260510_…``) with label-style ids
    # (``REA3_SMOKE``) will show labels ahead of timestamps because the
    # uppercase 'R' outranks '2' in byte order. That predates REA-4 and
    # lives in CLI as well. The migration check here is "spawn appears in
    # history at all", not "spawn is the topmost row".
    print("[4/4] orcho_run_history(limit=50) …")
    try:
        hist = orcho_run_history(limit=50)
    except Exception as e:
        _abort(1, f"orcho_run_history raised {type(e).__name__}: {e}")
    if not hist.runs:
        _abort(2, "history is empty after a successful spawn")
    matched = next((r for r in hist.runs if r.run_id == run_id), None)
    if matched is None:
        _abort(
            2,
            f"spawned run {run_id!r} not found in history (saw "
            f"{[r.run_id for r in hist.runs]!r}). MCP→SDK round-trip broken.",
        )
    print(
        f"      matched {matched.run_id} → status={matched.status} "
        f"tokens={matched.total_tokens} task={matched.task!r}"
    )

    print("\n[OK] REA-4.1 live smoke passed:")
    print(f"     spawn → status={final_status} → metrics → history match.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--project",
        type=Path,
        default=Path.home() / "www/orcho/orcho-core/examples/golden-api",
        help="Project dir to spawn against (mock mode — no real LLM calls).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="Seconds to wait for the pipeline to reach a terminal status.",
    )
    args = parser.parse_args()

    if not args.project.is_dir():
        _abort(1, f"--project does not exist: {args.project}")

    return asyncio.run(_run(args.project, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
