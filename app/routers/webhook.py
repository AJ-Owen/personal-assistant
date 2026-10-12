import secrets
import logging
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.ai import agent
from app.config import settings
from app.handlers import vault
from app import history

log = logging.getLogger(__name__)
router = APIRouter()
_bearer = HTTPBearer(auto_error=False)

# Fields tried in order when extracting text from a webhook payload.
# "transcription" is the Pebble Index 01's multipart field name (confirmed via
# help.repebble.com "Index Advanced Features (MCP, Webhook)"); the rest cover
# the JSON body sent by the web UI and other future clients.
CANDIDATE_FIELDS = ("transcription", "text", "transcript", "body", "content", "message", "note", "speech")


async def _check_auth(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> None:
    if settings.webhook_token and (
        not credentials
        or not secrets.compare_digest(credentials.credentials, settings.webhook_token)
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _extract_text(payload: dict) -> str | None:
    for key in CANDIDATE_FIELDS:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


async def _run_agent(text: str, entry: dict) -> None:
    # Verbatim capture first — durability must not depend on the agent choosing
    # to call a tool (see vault.record_capture).
    await vault.record_capture(text, source="webhook")
    try:
        await agent.run(text, entry=entry)
    except Exception:
        log.exception("Agent failed for %r", text)
        entry["status"] = "error"
    finally:
        history.flush()  # entry was mutated in place — persist the final state


@router.post("/webhook", dependencies=[Depends(_check_auth)])
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    content_type = request.headers.get("content-type", "")

    if content_type.startswith("multipart/form-data"):
        # Pebble Index 01 sends recordings as multipart/form-data, with fields
        # like "transcription", "audio" (audio/mp4), "recordedAt", "client".
        form = await request.form()
        payload = {key: value for key, value in form.multi_items() if isinstance(value, str)}
        log.info("Webhook multipart fields: %s", sorted(payload.keys()))
    else:
        raw_body = await request.body()
        log.info("Webhook raw body: %.300s", raw_body.decode("utf-8", errors="replace"))
        try:
            payload = await request.json()
        except Exception:
            payload = {}

    text = _extract_text(payload)

    if not text:
        log.warning("No extractable text in webhook payload")
        return {"status": "ignored", "text": None}

    entry = history.add_entry(text)
    background_tasks.add_task(_run_agent, text, entry)
    return {"status": "received", "text": text}
