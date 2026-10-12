"""Webhook capture agent: durability guarantees around the tool-use loop."""

import pytest

from app.ai import agent, llm, mcp
from app.config import settings
from app.handlers import vault
from tests.conftest import FakeClient, response, text_block, tool_block


@pytest.fixture
def vault_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    # No MCP sidecars in tests: the agent runs with the local vault tool only,
    # and vault writes take the direct-filesystem fallback.
    monkeypatch.setattr(mcp, "connected", lambda: False)
    monkeypatch.setattr(mcp, "has_tool", lambda name: False)
    return tmp_path


def _install(monkeypatch, responses):
    client = FakeClient(responses)
    monkeypatch.setattr(llm, "get_client", lambda: client)
    return client


def _entry():
    return {"input": "", "reply": None, "actions": [], "status": "pending"}


@pytest.mark.asyncio
async def test_no_tool_call_still_files_the_memo(vault_tmp, monkeypatch):
    """A turn that ends with plain text used to discard the memo and report
    "done" — two real captures were lost this way. It must now be filed."""
    _install(monkeypatch, [response("end_turn", text_block("Nothing to do here."))])

    entry = _entry()
    await agent.run("Send Carole the list of pla colours tonight at 10pm", entry=entry)

    inbox = (vault_tmp / settings.inbox_filename).read_text(encoding="utf-8")
    assert "Send Carole the list of pla colours" in inbox
    assert "unfiled" in inbox
    assert entry["status"] == "unfiled"
    assert entry["actions"] and entry["actions"][0]["error"] is True


@pytest.mark.asyncio
async def test_tool_call_path_files_once_and_reports_done(vault_tmp, monkeypatch):
    """The fallback must not double-write when the agent did act."""
    _install(monkeypatch, [
        response("tool_use", tool_block("write_to_vault", {"content": "oat milk", "label": "note"})),
        response("end_turn", text_block("saved")),
    ])

    entry = _entry()
    await agent.run("oat milk", entry=entry)

    inbox = (vault_tmp / settings.inbox_filename).read_text(encoding="utf-8")
    assert inbox.count("oat milk") == 1
    assert "unfiled" not in inbox
    assert entry["status"] == "done"


@pytest.mark.asyncio
async def test_record_capture_writes_verbatim_outside_the_processed_tree(vault_tmp):
    """The raw capture log is the durability floor — and living under archive/
    keeps it out of inbox processing and the daily summary."""
    await vault.record_capture("S note from index one.", source="webhook")

    logs = list((vault_tmp / settings.capture_log_path).glob("*.md"))
    assert len(logs) == 1
    assert "S note from index one." in logs[0].read_text(encoding="utf-8")

    rel = logs[0].relative_to(vault_tmp)
    assert vault.is_machine_path(rel)


@pytest.mark.asyncio
async def test_unfiled_path_pushes_a_notification(vault_tmp, monkeypatch):
    """The one case worth interrupting the user for: the agent didn't know what
    to do. Silence here is exactly how the July/August memos went unnoticed."""
    _install(monkeypatch, [response("end_turn", text_block("Not sure."))])

    pushes = []

    async def _fake_send(message, *, title=None, tags=None):
        pushes.append((message, title, tags))
        return "Notification sent."

    monkeypatch.setattr(agent.notify, "send", _fake_send)

    await agent.run("something ambiguous", entry=_entry())

    assert len(pushes) == 1
    assert "something ambiguous" in pushes[0][0]
    assert pushes[0][2] == "warning"


@pytest.mark.asyncio
async def test_notify_tool_offered_only_when_ntfy_configured(vault_tmp, monkeypatch):
    """No topic configured means the model is never shown a tool that can't work."""
    client = _install(monkeypatch, [response("end_turn", text_block("ok"))])
    monkeypatch.setattr(agent.notify, "enabled", lambda: True)

    await agent.run("hello", entry=_entry())

    names = [t["name"] for t in client.messages.calls[0]["tools"]]
    assert agent._NOTIFY_TOOL_NAME in names

    client = _install(monkeypatch, [response("end_turn", text_block("ok"))])
    monkeypatch.setattr(agent.notify, "enabled", lambda: False)

    await agent.run("hello", entry=_entry())

    names = [t["name"] for t in client.messages.calls[0]["tools"]]
    assert agent._NOTIFY_TOOL_NAME not in names
