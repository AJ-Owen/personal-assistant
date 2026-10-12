"""MCP session lifecycle and tool dispatch.

Owns the connections to all configured MCP sidecars and the flat tool→session
map discovered at startup. Everything else in the app talks to MCP tools
through this module's public functions — no reaching into private state.
"""

import asyncio
import logging
from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.config import settings

log = logging.getLogger(__name__)

# Sidecars restart under us (obsidian-mcp is memory-capped and restarts instead
# of bloating), which kills the sessions opened at app startup. Every call has a
# read timeout so a dead session fails instead of hanging, and a failed call
# triggers one reconnect + retry. `_generation` dedupes concurrent reconnects.
_CALL_TIMEOUT = timedelta(seconds=90)
_CONNECT_TIMEOUT = 30
_CLOSE_TIMEOUT = 10

_connections: list["_Connection"] = []
_tool_to_session: dict[str, ClientSession] = {}
_all_tools: list[dict[str, Any]] = []
_generation = 0
_reconnect_lock = asyncio.Lock()


def _mcp_tool_to_anthropic(tool: Any) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.inputSchema,
    }


class _Connection:
    """One MCP server connection, with its async context managers owned by a
    single dedicated task.

    streamablehttp_client/ClientSession are anyio task groups whose cancel
    scopes must be entered and exited by the same task. Closing them from a
    different task (as the old AsyncExitStack reconnect path did) corrupts the
    opener's cancel-scope stack — on 2026-07-19 that cancelled the FastAPI
    lifespan task mid-reconnect, which ran the lifespan finally block and
    silently shut down the APScheduler jobs. Here the contexts live entirely
    inside `_run`, and `close()` only signals that task and awaits it.
    """

    def __init__(self, url: str):
        self.url = url
        self.session: ClientSession | None = None
        self.tools: list[Any] = []
        self._ready = asyncio.Event()
        self._closing = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def open(self) -> bool:
        """Start the owning task and wait for the session to come up.
        Returns False (after cleaning up) if connecting fails or times out."""
        self._task = asyncio.create_task(self._run(), name=f"mcp:{self.url}")
        try:
            await asyncio.wait_for(self._ready.wait(), _CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            log.error("MCP connect timed out for %s", self.url)
        if self.session is None:
            await self.close()
            return False
        return True

    async def _run(self) -> None:
        try:
            async with streamablehttp_client(self.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    response = await session.list_tools()
                    self.tools = sorted(response.tools, key=lambda t: t.name)
                    self.session = session
                    self._ready.set()
                    await self._closing.wait()
        except Exception:
            if self.session is None:
                log.exception("MCP connection failed for %s", self.url)
            else:
                # Teardown of a dead transport after a sidecar restart —
                # confined to this task, expected to be noisy.
                log.warning("Error closing MCP connection %s (expected after a sidecar restart)", self.url)
        finally:
            self.session = None
            self._ready.set()

    async def close(self) -> None:
        """Signal the owning task to unwind its contexts and wait for it.
        Safe to call from any task; cancellation is delivered to the owner."""
        self._closing.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, _CLOSE_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("MCP connection %s did not close in time — cancelling", self.url)
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        except Exception:
            pass
        self._task = None


async def open_sessions() -> None:
    """Connect to all configured MCP servers and cache their tool lists."""
    global _connections, _tool_to_session, _all_tools
    if _connections:
        return

    urls = [
        u for u in [settings.todoist_mcp_url, settings.brave_search_mcp_url, settings.obsidian_mcp_url] if u
    ]

    connections: list[_Connection] = []
    tool_to_session: dict[str, ClientSession] = {}
    all_tools: list[dict[str, Any]] = []

    for url in urls:
        conn = _Connection(url)
        if not await conn.open():
            log.error("MCP startup failed for %s — skipping", url)
            continue
        connections.append(conn)
        for t in conn.tools:
            tool_to_session[t.name] = conn.session
        all_tools.extend(_mcp_tool_to_anthropic(t) for t in conn.tools)
        log.info("MCP connected %s — tools: %s", url, [t.name for t in conn.tools])

    _connections = connections
    _tool_to_session = tool_to_session
    _all_tools = all_tools


async def close_sessions() -> None:
    global _connections, _tool_to_session, _all_tools
    conns = _connections
    _connections = []
    _tool_to_session = {}
    _all_tools = []
    for conn in conns:
        await conn.close()


def connected() -> bool:
    return bool(_tool_to_session)


def has_tool(name: str) -> bool:
    return name in _tool_to_session


def tools() -> list[dict[str, Any]]:
    """All discovered MCP tools in Anthropic tool-definition format."""
    return list(_all_tools)


async def call_tool(name: str, inp: dict) -> str:
    """Call an MCP tool and flatten its text content into one string.

    On failure (timeout, dead session after a sidecar restart), reconnects all
    sessions once and retries the call before giving up.
    """
    failed_gen = _generation
    try:
        return await _call_once(name, inp)
    except Exception:
        log.exception("MCP call %s failed — reconnecting sessions and retrying once", name)
        await _reconnect(failed_gen)
        if name not in _tool_to_session:
            raise
        return await _call_once(name, inp)


async def _call_once(name: str, inp: dict) -> str:
    session = _tool_to_session[name]
    result = await session.call_tool(name, inp, read_timeout_seconds=_CALL_TIMEOUT)
    text_parts = [
        c.text for c in (result.content or [])
        if getattr(c, "type", None) == "text"
    ]
    return "\n".join(text_parts) or "Done."


async def _reconnect(failed_gen: int) -> None:
    """Tear down all sessions and reopen them. No-op if another task already
    reconnected since `failed_gen` was captured."""
    global _generation
    async with _reconnect_lock:
        if _generation != failed_gen:
            return

        await close_sessions()
        await open_sessions()
        _generation += 1
        log.info("MCP sessions reconnected (%d tools)", len(_tool_to_session))
