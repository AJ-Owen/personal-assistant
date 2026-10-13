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


# On a safety-classifier refusal the API re-runs the request on Anthropic's
# recommended fallback model for that refusal category, inside the same call.
# Without it a false positive just ends the run. Passed as raw header/body so it
# works on the regular messages.create with the pinned SDK.
FALLBACK_KWARGS: dict = {
    "extra_headers": {"anthropic-beta": "server-side-fallback-2026-07-01"},
    "extra_body": {"fallbacks": "default"},
}

# Content block types the app reads or echoes back. Anything else (e.g. the
# `fallback` audit marker a fallback inserts) is dropped before use.
KNOWN_BLOCK_TYPES = frozenset({"text", "thinking", "redacted_thinking", "tool_use"})


def known_blocks(content) -> list:
    return [b for b in content if getattr(b, "type", None) in KNOWN_BLOCK_TYPES]


def log_stop(log, source: str, response, max_tokens: int) -> None:
    """Log stop reasons that look like a clean finish but aren't."""
    if response.stop_reason == "max_tokens":
        # Thinking and response text share max_tokens; a truncated turn ends
        # exactly like a completed one, so without this it looks like success.
        log.error("%s hit max_tokens (%d) — output truncated, work may be incomplete", source, max_tokens)
    elif response.stop_reason == "refusal":
        log.error("%s refused by safety classifier: %s", source, getattr(response, "stop_details", None))


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
