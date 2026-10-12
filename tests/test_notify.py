"""ntfy push notifications — the assistant's only outbound channel to the user."""

import pytest

from app.config import settings
from app.handlers import notify


@pytest.fixture
def ntfy_on(monkeypatch):
    monkeypatch.setattr(settings, "ntfy_url", "http://ntfy:80")
    monkeypatch.setattr(settings, "ntfy_topic", "assistant")
    monkeypatch.setattr(settings, "ntfy_token", "tk_test")


class _FakeResponse:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """Stands in for httpx.AsyncClient as an async context manager."""

    def __init__(self, sink, response=None, exc=None):
        self.sink = sink
        self.response = response or _FakeResponse()
        self.exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, content=None, headers=None):
        if self.exc:
            raise self.exc
        self.sink.append({"url": url, "content": content, "headers": headers})
        return self.response


def _install(monkeypatch, sink, **kwargs):
    monkeypatch.setattr(notify.httpx, "AsyncClient", lambda **_: _FakeClient(sink, **kwargs))


def test_disabled_without_a_topic(monkeypatch):
    monkeypatch.setattr(settings, "ntfy_topic", "")
    assert notify.enabled() is False


@pytest.mark.asyncio
async def test_send_is_a_noop_when_disabled(monkeypatch):
    """No topic configured must degrade silently, not raise — the stack has to
    run unchanged without ntfy settings."""
    monkeypatch.setattr(settings, "ntfy_topic", "")
    sent: list = []
    _install(monkeypatch, sent)

    result = await notify.send("hello")

    assert sent == []
    assert "skipped" in result.lower()


@pytest.mark.asyncio
async def test_send_posts_to_topic_with_auth(ntfy_on, monkeypatch):
    sent: list = []
    _install(monkeypatch, sent)

    result = await notify.send("Eclipse is on Aug 12", title="Eclipse", tags="mag")

    assert result == "Notification sent."
    assert len(sent) == 1
    assert sent[0]["url"] == "http://ntfy:80/assistant"
    assert sent[0]["content"] == b"Eclipse is on Aug 12"
    assert sent[0]["headers"]["Authorization"] == "Bearer tk_test"
    assert sent[0]["headers"]["Title"] == "Eclipse"
    assert sent[0]["headers"]["Tags"] == "mag"


@pytest.mark.asyncio
async def test_non_ascii_title_does_not_break_headers(ntfy_on, monkeypatch):
    """Transcribed speech is routinely non-ascii; ntfy headers are not."""
    sent: list = []
    _install(monkeypatch, sent)

    await notify.send("body", title="Café 25° — naïve")

    assert sent[0]["headers"]["Title"].isascii()


@pytest.mark.asyncio
async def test_transport_failure_is_reported_not_raised(ntfy_on, monkeypatch):
    """A failed push must never fail the capture it was reporting on."""
    _install(monkeypatch, [], exc=RuntimeError("connection refused"))

    result = await notify.send("hello")

    assert "failed" in result.lower()


@pytest.mark.asyncio
async def test_http_error_is_reported_not_raised(ntfy_on, monkeypatch):
    _install(monkeypatch, [], response=_FakeResponse(403, "forbidden"))

    result = await notify.send("hello")

    assert "403" in result
