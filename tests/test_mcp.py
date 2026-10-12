import asyncio
from types import SimpleNamespace

import pytest

from app.ai import mcp


class FakeSession:
    def __init__(self, fail_times=0, reply="ok"):
        self.fail_times = fail_times
        self.reply = reply
        self.calls = 0

    async def call_tool(self, name, inp, read_timeout_seconds=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("session dead (sidecar restarted)")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)])


@pytest.mark.asyncio
async def test_call_tool_reconnects_and_retries_after_failure(monkeypatch):
    dead = FakeSession(fail_times=99)
    fresh = FakeSession(reply="recovered")
    monkeypatch.setattr(mcp, "_tool_to_session", {"search_notes": dead})
    monkeypatch.setattr(mcp, "_generation", 0)

    async def fake_open_sessions():
        mcp._tool_to_session = {"search_notes": fresh}

    monkeypatch.setattr(mcp, "open_sessions", fake_open_sessions)

    result = await mcp.call_tool("search_notes", {"query": "x"})

    assert result == "recovered"
    assert dead.calls == 1
    assert fresh.calls == 1


@pytest.mark.asyncio
async def test_call_tool_raises_when_tool_gone_after_reconnect(monkeypatch):
    dead = FakeSession(fail_times=99)
    monkeypatch.setattr(mcp, "_tool_to_session", {"search_notes": dead})
    monkeypatch.setattr(mcp, "_generation", 0)

    async def fake_open_sessions():
        mcp._tool_to_session = {}  # sidecar still down

    monkeypatch.setattr(mcp, "open_sessions", fake_open_sessions)

    with pytest.raises(ConnectionError):
        await mcp.call_tool("search_notes", {"query": "x"})


@pytest.mark.asyncio
async def test_concurrent_failures_reconnect_once(monkeypatch):
    dead = FakeSession(fail_times=99)
    fresh = FakeSession(reply="ok")
    monkeypatch.setattr(mcp, "_tool_to_session", {"t": dead})
    monkeypatch.setattr(mcp, "_generation", 0)

    reconnects = []

    async def fake_open_sessions():
        reconnects.append(1)
        mcp._tool_to_session = {"t": fresh}

    monkeypatch.setattr(mcp, "open_sessions", fake_open_sessions)

    import asyncio
    results = await asyncio.gather(mcp.call_tool("t", {}), mcp.call_tool("t", {}))

    assert list(results) == ["ok", "ok"]
    assert len(reconnects) == 1  # generation check dedupes the second reconnect


class TrackingTransport:
    """Fake streamablehttp_client context manager that records which task
    enters and exits it."""

    entered_by = None
    exited_by = None

    async def __aenter__(self):
        TrackingTransport.entered_by = asyncio.current_task()
        return (None, None, None)

    async def __aexit__(self, *exc):
        TrackingTransport.exited_by = asyncio.current_task()
        return False


class FakeLiveSession:
    def __init__(self, read, write):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        pass

    async def list_tools(self):
        tool = SimpleNamespace(name="ping", description="", inputSchema={})
        return SimpleNamespace(tools=[tool])

    async def call_tool(self, name, inp, read_timeout_seconds=None):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="pong")])


@pytest.mark.asyncio
async def test_connection_contexts_open_and_close_in_same_task(monkeypatch):
    """Regression: transport contexts are anyio cancel scopes and must be
    entered/exited by the same task. Closing them from a different task (the
    old AsyncExitStack reconnect) cancelled the lifespan task and killed the
    scheduler (2026-07-19)."""
    TrackingTransport.entered_by = None
    TrackingTransport.exited_by = None
    monkeypatch.setattr(mcp, "streamablehttp_client", lambda url: TrackingTransport())
    monkeypatch.setattr(mcp, "ClientSession", FakeLiveSession)

    conn = mcp._Connection("http://fake")
    assert await conn.open()
    assert conn.session is not None
    assert [t.name for t in conn.tools] == ["ping"]

    # Close from this task — the owning task must unwind its own contexts
    await conn.close()
    assert conn.session is None
    assert TrackingTransport.entered_by is TrackingTransport.exited_by
    assert TrackingTransport.entered_by is not asyncio.current_task()


@pytest.mark.asyncio
async def test_connection_open_failure_returns_false(monkeypatch):
    class BrokenTransport:
        async def __aenter__(self):
            raise ConnectionError("sidecar down")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(mcp, "streamablehttp_client", lambda url: BrokenTransport())
    conn = mcp._Connection("http://down")
    assert not await conn.open()
    assert conn.session is None
