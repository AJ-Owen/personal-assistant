"""Append-only audit log of every mutating action taken on the user's memos.

Every tool call that creates, edits, moves, deletes, or otherwise changes the
user's data (vault notes, Todoist tasks, archive operations) is recorded to a
dated markdown log in the vault. Read-only lookups (search/read/list/find/get)
are skipped — the log is a trail of *changes*, not queries. The log is never
overwritten or cleared, only appended, so the history of actions is preserved.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.handlers import vault

log = logging.getLogger(__name__)

# Tools that only read state — never recorded. Anything not matched here is
# treated as mutating, so a new write-capable tool is logged by default.
_READONLY_TOOLS = {
    "read_inbox", "read_note", "read_multiple_notes", "search_vault",
    "search_notes", "list_vault", "list_directory", "list_all_tags",
    "get_frontmatter", "get_notes_info", "get_vault_stats",
    "brave_web_search", "brave_local_search",
    "search", "fetch", "fetch-object", "list-workspaces", "user-info",
    "view-attachment", "analyze-project-health",
}
_READONLY_PREFIXES = ("find-", "get-")


def _is_mutating(tool: str) -> bool:
    if tool in _READONLY_TOOLS:
        return False
    if tool.startswith(_READONLY_PREFIXES):
        return False
    return True


def _detail(tool: str, inp: dict) -> str:
    """Best-effort one-line description of what an action targeted."""
    if not isinstance(inp, dict):
        return str(inp)[:200]

    # Note paths / content
    path = inp.get("path") or inp.get("filepath") or inp.get("note") or inp.get("destination")
    content = inp.get("content") or inp.get("processed_log") or inp.get("text")
    if path or content:
        parts = []
        if path:
            parts.append(f"`{path}`")
        if content:
            preview = " ".join(str(content).split())[:120]
            parts.append(f'"{preview}"')
        return " · ".join(parts)

    # Todoist task adds carry a list of task objects
    tasks = inp.get("tasks")
    if isinstance(tasks, list) and tasks:
        names = [t.get("content", "") for t in tasks if isinstance(t, dict)]
        return "; ".join(n for n in names if n) or str(tasks)[:120]

    name = inp.get("name") or inp.get("query")
    if name:
        return str(name)[:120]

    # Fall back to a compact rendering of the whole input.
    return " ".join(f"{k}={v}" for k, v in inp.items())[:200]


async def record(tool: str, inp: dict, result: str = "", *, source: str) -> None:
    """Append a single mutating action to the dated audit log. No-op for reads.

    Writes go through vault.append_note (obsidian-mcp preferred, direct fallback)
    so log files are created with the vault owner's uid and sync cleanly.
    """
    if not _is_mutating(tool):
        return
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    ts = now.strftime("%H:%M:%S UTC")
    rel_path = f"{settings.audit_log_path}/{date_str}.md"
    target = Path(settings.vault_path) / rel_path

    status = ""
    if isinstance(result, str) and result.startswith("Error"):
        status = f" — ⚠️ {result.splitlines()[0][:120]}"

    line = f"- `{ts}` · **{source}** · `{tool}` — {_detail(tool, inp)}{status}\n"
    if not target.exists() or target.stat().st_size == 0:
        line = f"# Action Log — {date_str}\n\n" + line
    await vault.append_note(rel_path, line)
