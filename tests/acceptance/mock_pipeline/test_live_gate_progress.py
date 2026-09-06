"""Fresh stdio clients observe real gate output before releasing the command."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from core.observability import events
from pipeline.verification_command import run_command
from pipeline.verification_contract import PlaceholderContext, VerificationContract
from pipeline.verification_progress import build_gate_progress_context

from tests.fixtures.mcp_workspace import init_git_repo, meta, write_run
from tests.fixtures.stdio import initialized_stdio_session

pytestmark = pytest.mark.mcp_integration


def _data(result):
    assert not result.isError, result
    return result.structuredContent or json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_fresh_mcp_reads_and_watches_live_gate(fake_workspace, tmp_path):
    project = tmp_path / "project"
    init_git_repo(project)
    run_id = "20260101_121212"
    write_run(fake_workspace, run_id, meta=meta(status="running", project=str(project)))
    run_dir = fake_workspace / "runspace" / "runs" / run_id
    advance, release = tmp_path / "advance", tmp_path / "release"
    program = f'''
import pathlib, sys, time
advance, release = pathlib.Path({str(advance)!r}), pathlib.Path({str(release)!r})
def wait_for(path):
    deadline = time.monotonic() + 40
    while not path.exists():
        if time.monotonic() > deadline: sys.exit(9)
        time.sleep(0.01)
sys.stdout.write("STDOUT-FIRST"); sys.stdout.flush()
sys.stderr.write("STDERR-FIRST"); sys.stderr.flush()
wait_for(advance)
sys.stderr.write("-SECOND"); sys.stderr.flush()
wait_for(release)
'''
    spec = {"run": [sys.executable, "-c", program], "timeout": 50}
    contract = VerificationContract.from_plugin(SimpleNamespace(
        verification={"commands": {"probe": spec}}, verification_envs={},
        dependency_repos={}, work_mode="",
    ))
    assert contract is not None
    context = PlaceholderContext(checkout=str(project), project=str(project))
    result = {}
    events.init_event_store(run_dir, resume=True)
    events.set_phase(None)

    def execute():
        events.emit("gate.start", name="probe", invocation_id="probe-1")
        try:
            result["receipt"] = run_command(
                "probe", spec, contract, context, log_dir=run_dir / "logs",
                progress=build_gate_progress_context(
                    name="probe", invocation_id="probe-1", hook="after_phase", phase="implement",
                ),
            )
            (run_dir / "probe-receipt.json").write_text(json.dumps(result["receipt"], default=str))
        finally:
            events.emit("gate.end", name="probe", invocation_id="probe-1", outcome="passed")

    worker = threading.Thread(target=execute)
    worker.start()
    try:
        async with initialized_stdio_session(fake_workspace) as (client, _):
            async with asyncio.timeout(20):
                while True:
                    card = _data(await client.call_tool("orcho_run_live_status", {"run_id": run_id}))
                    gate = card.get("active_gate")
                    if gate and "STDOUT-FIRST" in gate["stdout_tail"] and "STDERR-FIRST" in gate["stderr_tail"]:
                        break
                    await asyncio.sleep(0.05)
            assert worker.is_alive() and not release.exists()
            assert card["state_class"] == "running_gate"
            assert gate["phase"] == "implement"
            cursor = card["next_seq"]
            watch = asyncio.create_task(client.call_tool("orcho_run_watch", {
                "run_id": run_id, "since_seq": cursor, "until": "next_event", "timeout_s": 10,
            }))
            advance.touch()
            watched = _data(await asyncio.wait_for(watch, 15))
            assert watched["trigger"]["seq"] > cursor
            assert worker.is_alive() and not release.exists()
            card = _data(await client.call_tool("orcho_run_live_status", {"run_id": run_id}))
            assert "SECOND" in card["active_gate"]["stderr_tail"]
            release.touch()
            await asyncio.to_thread(worker.join, 10)
            assert not worker.is_alive()
            settled = _data(await client.call_tool("orcho_run_live_status", {"run_id": run_id}))
            assert settled["active_gate"] is None
            receipt = json.loads((run_dir / "probe-receipt.json").read_text())
            assert receipt["exit_code"] == 0 and receipt["outcome"] == "completed"
            assert "STDOUT-FIRST" in Path(receipt["log_path"]).read_text()
            assert "STDERR-FIRST-SECOND" in receipt["stderr_tail"]
    finally:
        advance.touch()
        release.touch()
        await asyncio.to_thread(worker.join, 10)
        events.init_event_store(None)
