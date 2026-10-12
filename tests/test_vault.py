import os
import time
from datetime import datetime, timezone

import pytest

from app.ai import mcp
from app.config import settings
from app.handlers import vault


def _archive_text(tmp_path):
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (tmp_path / settings.inbox_archive_path / f"{date_str}.md").read_text()


@pytest.mark.asyncio
async def test_archive_inbox_preserves_raw_verbatim(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))

    raw = "\n## 2026-06-20 09:00 UTC\nbuy milk and call the dentist\n"
    (tmp_path / settings.inbox_filename).write_text(raw, encoding="utf-8")

    staged = await vault.begin_inbox_processing()
    assert "buy milk and call the dentist" in staged

    await vault.archive_inbox("# Inbox Processing\n\nFiled everything.\n")

    archive = _archive_text(tmp_path)
    # Model summary is kept...
    assert "Filed everything." in archive
    # ...and the raw memo is preserved verbatim, independent of the summary.
    assert "buy milk and call the dentist" in archive
    assert "Raw captured entries" in archive

    # The staged working file is consumed; the live inbox was staged away.
    assert not (tmp_path / "inbox.processing.md").exists()
    assert not (tmp_path / settings.inbox_filename).exists()


@pytest.mark.asyncio
async def test_memo_arriving_mid_processing_survives(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))

    (tmp_path / settings.inbox_filename).write_text("\n## ts\noriginal memo\n", encoding="utf-8")
    await vault.begin_inbox_processing()

    # A webhook memo lands while the processor is running...
    await vault.append_to_vault("memo that arrived mid-run")

    await vault.archive_inbox("# log\n")

    # ...it is untouched by the archive/clear and waits for the next run.
    live = (tmp_path / settings.inbox_filename).read_text()
    assert "memo that arrived mid-run" in live
    archive = _archive_text(tmp_path)
    assert "original memo" in archive
    assert "memo that arrived mid-run" not in archive


@pytest.mark.asyncio
async def test_crash_leftovers_folded_into_next_run(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))

    # Leftover staged file from a crashed run + new memos in the live inbox
    (tmp_path / "inbox.processing.md").write_text("\n## ts1\nold staged memo\n", encoding="utf-8")
    (tmp_path / settings.inbox_filename).write_text("\n## ts2\nnew memo\n", encoding="utf-8")

    staged = await vault.begin_inbox_processing()
    assert "old staged memo" in staged
    assert "new memo" in staged


@pytest.mark.asyncio
async def test_archive_inbox_no_raw_block_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    (tmp_path / settings.inbox_filename).write_text("", encoding="utf-8")

    await vault.begin_inbox_processing()
    await vault.archive_inbox("# Inbox Processing\n\nNothing to do.\n")

    assert "Raw captured entries" not in _archive_text(tmp_path)


@pytest.mark.asyncio
async def test_append_prefers_mcp_write(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    calls = []

    async def fake_call_tool(name, inp):
        calls.append((name, inp))
        return "ok"

    monkeypatch.setattr(mcp, "has_tool", lambda name: name == "write_note")
    monkeypatch.setattr(mcp, "call_tool", fake_call_tool)

    await vault.append_to_vault("hello", label="idea")

    assert calls and calls[0][0] == "write_note"
    assert calls[0][1]["path"] == settings.inbox_filename
    assert calls[0][1]["mode"] == "append"
    assert "hello" in calls[0][1]["content"]
    # MCP handled it — no direct write happened
    assert not (tmp_path / settings.inbox_filename).exists()


@pytest.mark.asyncio
async def test_append_falls_back_to_direct_write_on_mcp_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))

    async def broken_call_tool(name, inp):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(mcp, "has_tool", lambda name: True)
    monkeypatch.setattr(mcp, "call_tool", broken_call_tool)

    await vault.append_to_vault("precious memo")

    # Capture never fails — the memo landed via the direct write
    assert "precious memo" in (tmp_path / settings.inbox_filename).read_text()


def test_overwrite_guard_refuses_existing_note(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes/x.md").write_text("precious content", encoding="utf-8")

    refusal = vault.overwrite_guard("write_note", {"path": "notes/x.md", "content": "new"})
    assert refusal is not None and "Refused" in refusal
    assert (tmp_path / "notes/x.md").read_text() == "precious content"


def test_overwrite_guard_allows_safe_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes/x.md").write_text("content", encoding="utf-8")

    # Appends and prepends to existing notes are fine
    assert vault.overwrite_guard("write_note", {"path": "notes/x.md", "mode": "append"}) is None
    assert vault.overwrite_guard("write_note", {"path": "notes/x.md", "mode": "prepend"}) is None
    # Creating a new note is fine
    assert vault.overwrite_guard("write_note", {"path": "notes/new.md"}) is None
    # Other tools are not the guard's business
    assert vault.overwrite_guard("patch_note", {"path": "notes/x.md"}) is None
    assert vault.overwrite_guard("delete_note", {"path": "notes/x.md"}) is None


def _write(p, mtime_epoch=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    if mtime_epoch is not None:
        os.utime(p, (mtime_epoch, mtime_epoch))


def test_list_recent_files_filters_by_age_and_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))
    now = time.time()
    old = now - 48 * 3600

    _write(tmp_path / "notes/daily/2026-06-19.md", now)        # recent → included
    _write(tmp_path / "notes/wiki/topic.md", now)              # recent → included
    _write(tmp_path / "notes/old.md", old)                     # too old → excluded
    _write(tmp_path / settings.inbox_filename, now)            # inbox → excluded
    _write(tmp_path / "inbox.processing.md", now)              # staged inbox → excluded
    _write(tmp_path / "archive/inbox/2026-06-18.md", now)      # archive dir → excluded
    _write(tmp_path / "archive/audit/2026-06-18.md", now)      # audit log → excluded
    _write(tmp_path / "meta/js/header.md", now)                # meta dir → excluded

    recent = vault.list_recent_files(hours=24)
    assert "notes/daily/2026-06-19.md" in recent
    assert "notes/wiki/topic.md" in recent
    assert "notes/old.md" not in recent
    assert settings.inbox_filename not in recent
    assert "inbox.processing.md" not in recent
    assert "archive/inbox/2026-06-18.md" not in recent
    assert "archive/audit/2026-06-18.md" not in recent
    assert "meta/js/header.md" not in recent
