"""
Keep the phone listener (`telegram-listen`) alive.

The listener is what hears /apply and what holds the PC awake. It runs as the Windows scheduled task
"Naukri Agent - Phone Control", started once at logon. When the PC went to sleep, Windows ended that task ("process
terminated unexpectedly", 0x8007042B) and nothing started it again: on 15 Oct and again on 22 Oct the listener was
dead from the first sleep of the day, the PC was then free to sleep, and /apply went unheard (the 22 Oct listener
ended at 09:29, when the PC slept 30 minutes after boot).

So every scheduled command that runs at a known time asks Task Scheduler whether the listener is running, and starts
the task again if it is not. A running task is left alone, and the task's own "do not start a second copy" rule makes
a double start harmless. Nothing here changes how the task is configured; it only asks for the task to be started,
exactly as `Start-ScheduledTask` would. Every failure is reported as a word and never raised: a problem with phone
control must never stop a job search.
"""

from __future__ import annotations

import csv
import io
import logging
import subprocess
import sys
import time
from typing import Callable

from naukri_agent.config import Settings

logger = logging.getLogger(__name__)

_VERIFY_SECONDS = 15


def _run_schtasks(args: list[str]) -> subprocess.CompletedProcess:
    # CREATE_NO_WINDOW: the guard runs hidden every 10 minutes, and a console flashing up each time would be a nuisance.
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def phone_control_status(task_name: str) -> str:
    """The task's Task Scheduler status ('Running', 'Ready', 'Disabled', ...), or 'unavailable' if it cannot be read."""
    try:
        done = _run_schtasks(["/query", "/tn", task_name, "/fo", "csv", "/nh"])
        if done.returncode != 0:
            return "unavailable"
        rows = [r for r in csv.reader(io.StringIO(done.stdout.strip())) if r]
        return rows[0][-1].strip() or "unavailable" if rows else "unavailable"
    except Exception as exc:  # noqa: BLE001 - "cannot read" is an answer, not a crash
        logger.warning("could not read the phone control task: %s", type(exc).__name__)
        return "unavailable"


def ensure_phone_control_running(
    settings: Settings, *, sleep: Callable[[float], None] = time.sleep, verify_seconds: float = _VERIFY_SECONDS
) -> str:
    """Make sure the phone listener's task is running. Returns one word:

    skipped      not Windows, phone control is switched off, or auto-restart is switched off
    running      it was already running (the usual answer)
    started      it was not running; it was started and Task Scheduler now reports it Running
    failed       it was not running and could not be started
    disabled     the task exists but is Disabled (a person turned it off; left alone)
    unavailable  Task Scheduler could not be asked, or the task does not exist
    """
    try:
        if (
            sys.platform != "win32"
            or not settings.phone_control_auto_restart
            or not settings.telegram_remote_start
            or not (settings.telegram_bot_token and settings.telegram_chat_id)
        ):
            return "skipped"
        name = settings.phone_control_task_name
        status = phone_control_status(name)
        if status in ("Running", "Queued"):
            return "running"
        if status == "Disabled":
            return "disabled"
        if status != "Ready":
            return "unavailable"
        started = _run_schtasks(["/run", "/tn", name])
        if started.returncode != 0:
            logger.warning("phone control was not running and could not be started (schtasks exit %s)", started.returncode)
            return "failed"
        waited = 0.0
        while waited <= verify_seconds:
            if phone_control_status(name) == "Running":
                logger.info("phone control (the /apply listener) was not running; started it again")
                return "started"
            sleep(1.0)
            waited += 1.0
        logger.warning("phone control was started but Task Scheduler does not report it running")
        return "failed"
    except Exception as exc:  # noqa: BLE001 - never stop a job search over this
        logger.warning("could not check phone control: %s", type(exc).__name__)
        return "unavailable"
