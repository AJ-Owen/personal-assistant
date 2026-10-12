import logging
from fastapi import APIRouter, BackgroundTasks, Depends

from app.processors.daily_summary import generate_daily_summary
from app.processors.inbox import process_inbox
from app.routers.webhook import _check_auth

log = logging.getLogger(__name__)
router = APIRouter()


async def _run_inbox() -> None:
    try:
        await process_inbox()
    except Exception:
        log.exception("Manual inbox processing failed")


async def _run_daily_summary() -> None:
    try:
        await generate_daily_summary()
    except Exception:
        log.exception("Manual daily summary failed")


@router.post("/process", dependencies=[Depends(_check_auth)])
async def trigger_processing(background_tasks: BackgroundTasks):
    """Manually trigger inbox processing ahead of the scheduled run."""
    background_tasks.add_task(_run_inbox)
    return {"status": "started"}


@router.post("/daily-summary", dependencies=[Depends(_check_auth)])
async def trigger_daily_summary(background_tasks: BackgroundTasks):
    """Manually trigger today's daily summary generation."""
    background_tasks.add_task(_run_daily_summary)
    return {"status": "started"}
