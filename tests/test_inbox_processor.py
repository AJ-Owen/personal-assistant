from datetime import datetime, timezone

import pytest

from app.ai import llm, mcp
from app.config import settings
from app.processors.inbox import process_inbox
from tests.conftest import FakeClient, response, text_block, tool_block


@pytest.fixture
def vault_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    return tmp_path


@pytest.fixture
def obsidian_mcp_up(monkeypatch):
    monkeypatch.setattr(mcp, "has_tool", lambda name: name == "search_notes")
    monkeypatch.setattr(mcp, "tools", lambda: [])


def _install(monkeypatch, responses):
    client = FakeClient(responses)
    monkeypatch.setattr(llm, "get_client", lambda: client)
    return client


def _archive_text(tmp_path):
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (tmp_path / settings.inbox_archive_path / f"{date_str}.md").read_text()


@pytest.mark.asyncio
async def test_processes_and_archives(vault_tmp, obsidian_mcp_up, monkeypatch):
    (vault_tmp / settings.inbox_filename).write_text("\n## ts\nbuy milk\n", encoding="utf-8")

    _install(monkeypatch, [
        response("tool_use", tool_block("archive_inbox", {"processed_log": "# log\nfiled milk"})),
        response("end_turn", text_block("done")),
    ])

    await process_inbox()

    archive = _archive_text(vault_tmp)
    assert "filed milk" in archive
    assert "buy milk" in archive  # verbatim snapshot
    assert not (vault_tmp / "inbox.processing.md").exists()


@pytest.mark.asyncio
async def test_safety_archive_when_model_never_archives(vault_tmp, obsidian_mcp_up, monkeypatch):
    (vault_tmp / settings.inbox_filename).write_text("\n## ts\nimportant memo\n", encoding="utf-8")

    # Model ends immediately without calling archive_inbox
    _install(monkeypatch, [response("end_turn", text_block("nothing to do"))])

    await process_inbox()

    archive = _archive_text(vault_tmp)
    assert "before archive_inbox was called" in archive
    assert "important memo" in archive  # raw memo still preserved
    assert not (vault_tmp / "inbox.processing.md").exists()


@pytest.mark.asyncio
async def test_skips_without_obsidian_mcp(vault_tmp, monkeypatch):
    (vault_tmp / settings.inbox_filename).write_text("\n## ts\nmemo\n", encoding="utf-8")
    monkeypatch.setattr(mcp, "has_tool", lambda name: False)
    client = _install(monkeypatch, [])

    await process_inbox()

    # No model calls, inbox untouched — the run waits for the sidecar
    assert client.messages.calls == []
    assert "memo" in (vault_tmp / settings.inbox_filename).read_text()


@pytest.mark.asyncio
async def test_noop_when_nothing_to_process(vault_tmp, obsidian_mcp_up, monkeypatch):
    client = _install(monkeypatch, [])
    await process_inbox()
    assert client.messages.calls == []
