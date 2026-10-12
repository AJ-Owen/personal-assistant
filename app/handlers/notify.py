"""Push notifications to the user's phone via a self-hosted ntfy server.

This is the assistant's only outbound channel to the user. The Pebble Index 01
webhook is strictly one-way — `/webhook` returns before the background agent has
run, so nothing the agent produces can reach the ring in the HTTP response. A
push closes that loop: ask a question from the ring, get the answer on your phone.

Reached at `http://ntfy:80` over the shared docker_proxy_network (the ntfy
container is on it), so this needs no internet egress from the app container.
Auth is a dedicated write-only token scoped to a single topic — deliberately not
the `monitor` credential that fail2ban and Beszel publish under.
"""

import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_TIMEOUT = 10.0


def enabled() -> bool:
    """True when a topic is configured. Everything is a no-op otherwise, so the
    stack runs unchanged (and tests import cleanly) with no ntfy settings."""
    return bool(settings.ntfy_topic)


async def send(message: str, *, title: str | None = None, tags: str | None = None) -> str:
    """Publish one notification. Returns a short status string for the model.

    Never raises: a failed push must not fail the capture it was reporting on.
    """
    if not enabled():
        return "Notification skipped: no ntfy topic configured."

    headers: dict[str, str] = {}
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    if title:
        # ntfy headers must be latin-1 safe; transcribed text often is not.
        headers["Title"] = title.encode("ascii", "replace").decode("ascii")
    if tags:
        headers["Tags"] = tags

    url = f"{settings.ntfy_url.rstrip('/')}/{settings.ntfy_topic}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, content=message.encode("utf-8"), headers=headers)
        if resp.status_code >= 400:
            log.error("ntfy publish failed: %s %.200s", resp.status_code, resp.text)
            return f"Notification failed (HTTP {resp.status_code})."
        log.info("ntfy notification sent: %.80s", message)
        return "Notification sent."
    except Exception as exc:
        log.exception("ntfy publish error")
        return f"Notification failed: {exc}"
