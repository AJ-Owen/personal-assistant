"""Shared Anthropic client and system-prompt assembly."""

from pathlib import Path

import anthropic

from app.config import settings

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None)
    return _client


def load_user_context() -> str:
    """Personal context markdown, re-read each call so edits apply without restart."""
    path = settings.user_context_path
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def build_system_prompt(instructions: str) -> str:
    user_ctx = load_user_context()
    if user_ctx:
        return f"## About the user\n{user_ctx}\n\n---\n\n{instructions}"
    return instructions
