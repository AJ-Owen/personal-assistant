"""In-memory activity feed with optional JSONL persistence across restarts."""

import json
import logging
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)

_store: deque[dict] = deque(maxlen=20)
_loaded = False


def _path() -> Path | None:
    return Path(settings.history_path) if settings.history_path else None


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    p = _path()
    if p is None or not p.exists():
        return
    try:
        # File is oldest-first; appendleft keeps the newest entry at the front.
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                _store.appendleft(json.loads(line))
    except (OSError, ValueError):
        log.exception("Failed to load history from %s", p)


def flush() -> None:
    """Persist the feed. Entries mutate in place after add_entry (status,
    actions, reply), so callers re-flush when an agent run finishes."""
    p = _path()
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(e) for e in reversed(_store)]
        p.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")
    except OSError:
        log.exception("Failed to write history to %s", p)


def add_entry(text: str) -> dict:
    _ensure_loaded()
    entry = {
        "id": uuid.uuid4().hex[:8],
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
        "input": text,
        "reply": None,
        "actions": [],
        "status": "pending",
    }
    _store.appendleft(entry)
    flush()
    return entry


def recent() -> list[dict]:
    _ensure_loaded()
    return list(_store)
