"""
Stage B: in-process daily scheduler.

`naukri-agent scheduler` starts a long-running foreground process that
fires `run_daily_recommendations` once a day at `Settings.daily_run_time`
(HH:MM, 24-hour) in `Settings.timezone` — a cross-platform alternative to
an OS-level cron entry / Windows Task Scheduler task calling
`naukri-agent run-daily` on its own schedule. Either approach is valid;
this module exists for whoever would rather not depend on OS-level
scheduling infrastructure.

This process is NOT how the daily run reaches DailyRun/RunEvent rows in
the database, or how it sends email — those are entirely
`run_daily_recommendations`'s job (orchestration/pipeline.py), unchanged
here. This module's only responsibility is "call that function once a
day, and never let one bad day take down tomorrow's run."
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from naukri_agent.config import Settings, get_settings
from naukri_agent.orchestration.pipeline import run_daily_recommendations

logger = logging.getLogger(__name__)

JOB_ID = "daily_recommendations"


def _parse_daily_run_time(value: str) -> tuple[int, int]:
    """
    Settings' own validator already normalizes daily_run_time to a
    strict "HH:MM" string, so this never raises in practice for a
    Settings instance that passed validation — kept as a narrow parse
    step (not a re-validation) so this module has one place that knows
    the string's shape.
    """
    hour_str, minute_str = value.split(":")
    return int(hour_str), int(minute_str)


def _run_job_safely(settings: Settings, job_fn) -> None:
    """
    Run one scheduled firing. Never lets an exception escape — a
    BlockingScheduler job that raises has its FUTURE firings cancelled
    by APScheduler by default, which would silently turn "daily" into
    "once". Logging only the exception TYPE matches this project's
    existing convention (see notifications/email.py) of never risking
    an echoed secret/request fragment in a log line.
    """
    try:
        result = job_fn(settings=settings)
        logger.info(
            "scheduled daily run finished: run_id=%s status=%s recommendations=%s",
            result.run_id, result.status, result.recommendations,
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        logger.error("scheduled daily run raised %s; will retry at the next firing", type(exc).__name__)


def build_scheduler(
    settings: Settings | None = None, job_fn=run_daily_recommendations
) -> BlockingScheduler:
    """
    Construct (but do not start) a BlockingScheduler with one daily
    cron job. Kept separate from run_scheduler() so tests can inspect
    the configured trigger without ever calling the blocking start().
    """
    settings = settings or get_settings()
    hour, minute = _parse_daily_run_time(settings.daily_run_time)

    scheduler = BlockingScheduler(timezone=settings.timezone)
    scheduler.add_job(
        _run_job_safely,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=settings.timezone),
        args=[settings, job_fn],
        id=JOB_ID,
        name="naukri-agent daily recommendations",
        misfire_grace_time=3600,
        coalesce=True,
    )
    return scheduler


def run_scheduler(settings: Settings | None = None, job_fn=run_daily_recommendations) -> None:
    """
    Start the scheduler and block forever, firing job_fn once a day.
    Intended to be run as `naukri-agent scheduler` in its own terminal /
    service unit — this call does not return until interrupted.
    """
    settings = settings or get_settings()
    scheduler = build_scheduler(settings, job_fn)
    logger.info(
        "starting daily scheduler: %s daily at %s (%s)",
        JOB_ID, settings.daily_run_time, settings.timezone,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler stopping (interrupted)")
        scheduler.shutdown(wait=False)
