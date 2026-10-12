from datetime import datetime, timezone

import pytest

from app.config import settings
from app.handlers import audit


@pytest.mark.parametrize("tool", [
    "find-tasks", "find-projects", "get-overview", "search", "fetch",
    "read_note", "search_vault", "list_vault", "brave_web_search", "user-info",
])
def test_readonly_tools_not_mutating(tool):
    assert audit._is_mutating(tool) is False


@pytest.mark.parametrize("tool", [
    "write_to_vault", "create_note", "append_to_note", "move_note", "delete_note",
    "patch_note", "add-tasks", "complete-tasks", "archive_inbox", "some-new-tool",
])
def test_write_tools_are_mutating(tool):
    assert audit._is_mutating(tool) is True


def test_detail_extracts_path_and_content():
    out = audit._detail("create_note", {"path": "notes/x.md", "content": "hello   world"})
    assert "notes/x.md" in out
    assert "hello world" in out


def test_detail_extracts_todoist_task_names():
    out = audit._detail("add-tasks", {"tasks": [{"content": "buy milk"}, {"content": "call mum"}]})
    assert "buy milk" in out and "call mum" in out


@pytest.mark.asyncio
async def test_record_writes_mutating_action(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    await audit.record("create_note", {"path": "notes/x.md", "content": "hi"}, "Created", source="webhook")

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = tmp_path / settings.audit_log_path / f"{date_str}.md"
    assert log_file.exists()
    text = log_file.read_text()
    assert "# Action Log" in text
    assert "create_note" in text
    assert "webhook" in text


@pytest.mark.asyncio
async def test_record_skips_readonly(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    await audit.record("search_vault", {"query": "foo"}, "results", source="webhook")

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = tmp_path / settings.audit_log_path / f"{date_str}.md"
    assert not log_file.exists()


@pytest.mark.asyncio
async def test_record_appends_not_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    await audit.record("create_note", {"path": "a.md", "content": "1"}, source="webhook")
    await audit.record("append_to_note", {"path": "b.md", "content": "2"}, source="inbox-processor")

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = (tmp_path / settings.audit_log_path / f"{date_str}.md").read_text()
    assert text.count("# Action Log") == 1  # header written once
    assert "a.md" in text and "b.md" in text
    assert "inbox-processor" in text


@pytest.mark.asyncio
async def test_record_flags_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    await audit.record("delete_note", {"path": "x.md"}, "Error: not found", source="webhook")

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = (tmp_path / settings.audit_log_path / f"{date_str}.md").read_text()
    assert "⚠️" in text and "not found" in text
