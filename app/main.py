import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.ai import mcp
from app import scheduler
from app.routers import chat, process, ui, webhook

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
for _noisy in ("httpx", "httpcore", "anthropic", "mcp"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await mcp.open_sessions()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()
        await mcp.close_sessions()


app = FastAPI(title="Personal Assistant", version="0.2.0", lifespan=lifespan)
app.include_router(webhook.router)
app.include_router(ui.router)
app.include_router(process.router)
app.include_router(chat.router)
