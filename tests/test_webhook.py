import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import webhook
from app.routers.webhook import _extract_text, CANDIDATE_FIELDS


@pytest.mark.asyncio
async def test_background_task_records_capture_before_running_agent(monkeypatch):
    """Durability must not depend on the agent: record_capture has to run, and
    has to run first, even if the agent then blows up."""
    order = []

    async def _fake_capture(text, *, source):
        order.append(("capture", text, source))

    async def _fake_run(text, entry=None):
        order.append(("agent", text))
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(webhook.vault, "record_capture", _fake_capture)
    monkeypatch.setattr(webhook.agent, "run", _fake_run)

    entry = {"actions": [], "status": "pending"}
    await webhook._run_agent("buy oat milk", entry)

    assert order[0] == ("capture", "buy oat milk", "webhook")
    assert order[1][0] == "agent"
    assert entry["status"] == "error"


@pytest.mark.parametrize("field", CANDIDATE_FIELDS)
def test_extract_text_candidate_fields(field):
    assert _extract_text({field: "hello"}) == "hello"


def test_extract_text_priority():
    # "text" comes before "body" in CANDIDATE_FIELDS
    result = _extract_text({"text": "first", "body": "second"})
    assert result == "first"


def test_extract_text_whitespace_stripped():
    assert _extract_text({"text": "  hi there  "}) == "hi there"


def test_extract_text_empty_string_skipped():
    assert _extract_text({"text": "", "body": "fallback"}) == "fallback"


def test_extract_text_whitespace_only_skipped():
    assert _extract_text({"text": "   ", "body": "fallback"}) == "fallback"


def test_extract_text_none_value_skipped():
    assert _extract_text({"text": None, "body": "fallback"}) == "fallback"


def test_extract_text_non_string_skipped():
    assert _extract_text({"text": 42, "body": "fallback"}) == "fallback"


def test_extract_text_no_match():
    assert _extract_text({"unknown_key": "data"}) is None


def test_extract_text_empty_payload():
    assert _extract_text({}) is None


def test_webhook_accepts_pebble_multipart(monkeypatch):
    """Pebble Index 01 posts multipart/form-data, not JSON — the "transcription"
    field must be picked up (regression check for the JSON-only parsing bug)."""

    async def _fake_run(text, entry=None):
        return None

    monkeypatch.setattr(webhook.agent, "run", _fake_run)
    # The test service inherits the deployed .env, so a real WEBHOOK_TOKEN would
    # otherwise 401 this request. Auth is covered separately — pin it off here so
    # the test exercises multipart parsing and nothing else.
    monkeypatch.setattr(webhook.settings, "webhook_token", "")

    app = FastAPI()
    app.include_router(webhook.router)

    with TestClient(app) as client:
        response = client.post(
            "/webhook",
            # httpx only encodes as multipart/form-data (like the Pebble) when
            # using `files=`; (None, value) sends a field with no filename.
            files={
                "transcription": (None, "buy oat milk"),
                "recordedAt": (None, "1234567890"),
                "client": (None, "ring"),
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "received", "text": "buy oat milk"}
