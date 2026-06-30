"""Stdio coverage for MCP ``resources/subscribe`` notifications."""
from __future__ import annotations

import json
import os
import sys

import anyio
import pytest

pytest.importorskip("mcp.client.stdio")

from mcp import ClientSession, StdioServerParameters, types  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from pydantic import AnyUrl  # noqa: E402

from tests.fixtures.mcp_workspace import event, meta, write_run  # noqa: E402


@pytest.mark.anyio
async def test_resource_subscribe_emits_update_when_resource_changes(fake_workspace):
    run_id = "20260101_000001"
    run_dir = write_run(
        fake_workspace,
        run_id,
        meta=meta(status="running"),
        events=[event(1, "run.start")],
    )
    uri = f"orcho://runs/{run_id}/summary"
    notifications: list[str] = []
    updated = anyio.Event()

    async def record_message(message) -> None:
        if not isinstance(message, types.ServerNotification):
            return
        if isinstance(message.root, types.ResourceUpdatedNotification):
            notifications.append(str(message.root.params.uri))
            updated.set()

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "orcho_mcp"],
        env={**os.environ, "ORCHO_WORKSPACE": str(fake_workspace)},
    )
    async with stdio_client(params) as (read, write), ClientSession(
        read,
        write,
        message_handler=record_message,
    ) as session:
        init_result = await session.initialize()
        assert init_result.capabilities.resources is not None
        assert init_result.capabilities.resources.subscribe is True

        await session.subscribe_resource(AnyUrl(uri))
        events_path = run_dir / "events.jsonl"
        events_path.write_text(
            "".join(
                json.dumps(row) + "\n"
                for row in [
                    event(1, "run.start"),
                    event(2, "phase.start", phase="plan"),
                ]
            ),
            encoding="utf-8",
        )

        with anyio.fail_after(3):
            await updated.wait()

    assert notifications == [uri]


@pytest.fixture
def anyio_backend():
    return "asyncio"
