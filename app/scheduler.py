"""Hourly crawl on APScheduler. Started by the web app (step 3) or run standalone."""

from __future__ import annotations

import logging
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.crawler import run_crawl
from app.models import utcnow

log = logging.getLogger(__name__)

JOB_ID = "hourly-crawl"


def _job() -> None:
    try:
        run_crawl("scheduled")
    except Exception:  # a crash here would kill the job, not just the run
        log.exception("scheduled crawl failed")


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.tz)
    scheduler.add_job(
        _job,
        "interval",
        minutes=settings.crawl_interval_minutes,
        id=JOB_ID,
        # Give the process a minute to settle before the first crawl.
        next_run_time=utcnow() + timedelta(minutes=1),
        # A long crawl or a restart must not silently swallow the next tick.
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    return scheduler


if __name__ == "__main__":  # pragma: no cover
    import time

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    sched = create_scheduler()
    sched.start()
    log.info("scheduler started, crawling every %d min", settings.crawl_interval_minutes)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        sched.shutdown()
