import logging
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _run_inbox_processing() -> None:
    from app.processors.inbox import process_inbox
    log.info("Scheduled inbox processing starting")
    try:
        await process_inbox()
        log.info("Scheduled inbox processing complete")
    except Exception:
        log.exception("Scheduled inbox processing failed")


async def _run_daily_summary() -> None:
    from app.processors.daily_summary import generate_daily_summary
    log.info("Scheduled daily summary starting")
    try:
        await generate_daily_summary()
        log.info("Scheduled daily summary complete")
    except Exception:
        log.exception("Scheduled daily summary failed")


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return

    tz_name = os.getenv("TZ", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        log.warning("Unknown timezone %r — falling back to UTC", tz_name)
        tz = ZoneInfo("UTC")

    _scheduler = AsyncIOScheduler(timezone=tz)
    _scheduler.add_job(
        _run_inbox_processing,
        trigger="cron",
        hour=settings.process_hour,
        minute=0,
        id="inbox_processing",
        misfire_grace_time=3600,
    )
    _scheduler.add_job(
        _run_daily_summary,
        trigger="cron",
        hour=settings.daily_summary_hour,
        minute=0,
        id="daily_summary",
        misfire_grace_time=3600,
    )
    _scheduler.start()
    log.info(
        "Scheduler started — inbox processing at %02d:00 %s, daily summary at %02d:00 %s",
        settings.process_hour,
        tz_name,
        settings.daily_summary_hour,
        tz_name,
    )


def stop() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
