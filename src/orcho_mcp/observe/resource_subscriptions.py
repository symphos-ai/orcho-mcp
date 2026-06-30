"""MCP resource subscription support for ``orcho://*`` resources.

The high-level Orcho live-status path remains ``orcho_run_watch`` because it
returns bounded summaries, handoff hints, and progress cursors. This module
adds the standard MCP resource-subscription path for clients that subscribe to
specific resource URIs and expect ``notifications/resources/updated`` when the
URI body changes.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import NotificationOptions, request_ctx
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server
from pydantic import AnyUrl

_POLL_INTERVAL_S = 0.5


async def run_stdio_with_resource_notifications(mcp: FastMCP) -> None:
    """Run stdio with resource subscription/list-change capabilities enabled.

    FastMCP exposes the underlying handlers for ``resources/subscribe`` and
    notification helpers, but its stdio shortcut currently builds initialize
    capabilities with resource subscriptions disabled. Keep the same stdio
    transport and only adjust the initialization options.
    """
    options = mcp._mcp_server.create_initialization_options(  # noqa: SLF001
        notification_options=NotificationOptions(resources_changed=True),
    )
    if options.capabilities.resources is not None:
        options.capabilities.resources.subscribe = True
        options.capabilities.resources.listChanged = True

    async with stdio_server() as (read_stream, write_stream):
        await mcp._mcp_server.run(  # noqa: SLF001
            read_stream,
            write_stream,
            options,
        )


def register_resource_subscription_handlers(mcp: FastMCP) -> None:
    """Register low-level ``resources/subscribe`` handlers once."""
    server = mcp._mcp_server  # noqa: SLF001
    if getattr(server, "_orcho_resource_subscription_handlers", False):
        return

    registry = _ResourceSubscriptionRegistry(mcp)

    @server.subscribe_resource()
    async def _subscribe_resource(uri: AnyUrl) -> None:
        await registry.subscribe(str(uri))

    @server.unsubscribe_resource()
    async def _unsubscribe_resource(uri: AnyUrl) -> None:
        await registry.unsubscribe(str(uri))

    server._orcho_resource_subscription_handlers = True


@dataclass
class _SessionSubscriptions:
    session: ServerSession
    tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)


class _ResourceSubscriptionRegistry:
    def __init__(self, mcp: FastMCP) -> None:
        self._mcp = mcp
        self._sessions: dict[int, _SessionSubscriptions] = {}

    async def subscribe(self, uri: str) -> None:
        session = request_ctx.get().session
        state = self._sessions.setdefault(
            id(session),
            _SessionSubscriptions(session=session),
        )
        if uri in state.tasks:
            return

        initial = await self._fingerprint(uri)
        state.tasks[uri] = asyncio.create_task(
            self._poll_resource(state, uri, initial),
            name=f"orcho-mcp-resource-subscription:{uri}",
        )

    async def unsubscribe(self, uri: str) -> None:
        session = request_ctx.get().session
        state = self._sessions.get(id(session))
        if state is None:
            return
        task = state.tasks.pop(uri, None)
        if task is not None:
            task.cancel()
        if not state.tasks:
            self._sessions.pop(id(session), None)

    async def _poll_resource(
        self,
        state: _SessionSubscriptions,
        uri: str,
        last_seen: str,
    ) -> None:
        try:
            while True:
                await asyncio.sleep(_POLL_INTERVAL_S)
                current = await self._fingerprint(uri)
                if current == last_seen:
                    continue
                last_seen = current
                await state.session.send_resource_updated(AnyUrl(uri))
        except asyncio.CancelledError:
            raise
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            self._drop_session(state)
        except RuntimeError:
            self._drop_session(state)

    async def _fingerprint(self, uri: str) -> str:
        hasher = hashlib.sha256()
        try:
            contents = await self._mcp.read_resource(uri)
        except Exception as exc:  # noqa: BLE001
            hasher.update(type(exc).__name__.encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(str(exc).encode("utf-8", errors="replace"))
            return hasher.hexdigest()

        for item in contents:
            hasher.update((item.mime_type or "").encode("utf-8"))
            hasher.update(b"\0")
            content = item.content
            if isinstance(content, bytes):
                hasher.update(content)
            else:
                hasher.update(str(content).encode("utf-8", errors="replace"))
            hasher.update(b"\0")
        return hasher.hexdigest()

    def _drop_session(self, state: _SessionSubscriptions) -> None:
        self._sessions.pop(id(state.session), None)
        for task in state.tasks.values():
            task.cancel()


__all__ = [
    "register_resource_subscription_handlers",
    "run_stdio_with_resource_notifications",
]
