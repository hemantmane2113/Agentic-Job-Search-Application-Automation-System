"""
The phone guard: every few minutes, keep phone control alive; once a day, say that the PC is alive.

    naukri-agent phone-guard            # run by the scheduled task "Naukri Agent - Phone Guard", every 10 minutes 06:00-23:00
    naukri-agent phone-guard --dry-run  # shows what it would do; starts and sends nothing

Why it exists: Windows ends the phone listener's task whenever the PC sleeps or the network blips, and the scheduled
morning jobs only revive it at 09:45 and when they run. A glitch in the afternoon could leave /apply unheard until the next
morning. With the owner away from the PC for weeks that is not acceptable, so this tiny command checks every 10 minutes
(see phone_control.py) and starts the listener again within minutes.

The daily "alive" message exists because a PC that has frozen, been switched off or lost the network cannot send an alert:
SILENCE is the only signal. Once a day, at `alive_message_time` (default 10:50, after the digest is normally out), it sends one
Telegram message: the PC is on, phone control is running, what the job search did today, and how many jobs wait for /apply.
If that message does not arrive, something is wrong with the PC or its internet and someone should look.

It only ever SENDS (never reads) Telegram, so it cannot compete with the listener for incoming messages. It opens no
browser, imports no apply code, and a failure in it is reported as a word and never raised.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from naukri_agent.config import Settings
from naukri_agent.orchestration.phone_control import ensure_phone_control_running, phone_control_status

logger = logging.getLogger(__name__)


class GuardResult(BaseModel):
    phone_control: str = "unknown"  # the word from phone_control.ensure_phone_control_running
    alive_sent: bool = False
    down_alerted: bool = False
    notes: list[str] = Field(default_factory=list)
    preview: str | None = None  # --dry-run: the alive message that would be sent


def _local(settings: Settings, epoch: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(epoch, ZoneInfo(settings.timezone))


def _hm(text: str) -> tuple[int, int]:
    hour, minute = (int(p) for p in text.split(":"))
    return hour, minute


def _load_state(path: Path, today: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("date") == today:
            return data
    except Exception:  # noqa: BLE001 - missing or corrupt memory just means "nothing said yet today"
        pass
    return {"date": today}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:  # worst case a message is sent twice; never fatal
        logger.warning("could not save the phone guard state: %s", type(exc).__name__)


def alive_due(settings: Settings, epoch: float, state: dict[str, Any]) -> bool:
    """Once the alive time has passed today, until the awake window ends, and not already sent today."""
    if not settings.alive_message_enabled or state.get("alive_sent"):
        return False
    local = _local(settings, epoch)
    hm = (local.hour, local.minute)
    return _hm(settings.alive_message_time) <= hm < _hm(settings.telegram_listener_awake_until)


# --- what the message says ------------------------------------------------------------------------------------------------------


def gather_status(
    settings: Settings,
    now: datetime.datetime,
    *,
    session_factory: Any = None,
    task_info_fn: Callable[[str], Any] | None = None,
    ready_fn: Callable[[Settings, datetime.datetime], tuple[int, int, int]] | None = None,
) -> dict[str, Any]:
    """Read-only facts about today for the alive message. A busy database is an answer ("still running"), not an error."""
    from sqlalchemy.exc import OperationalError

    out: dict[str, Any] = {"run": "unknown", "finished": None, "new_jobs": None, "read": None, "reason": None,
                           "recommendations": None, "ready": None, "remaining": None, "cap": None}
    tz = ZoneInfo(settings.timezone)
    today = now.astimezone(tz).date()
    try:
        from naukri_agent.database.base import init_db, session_scope
        from naukri_agent.database.models import DailyRun, JobRecommendation

        factory = session_factory or init_db(settings)
        with session_scope(factory) as s:
            todays = None
            for run in s.query(DailyRun).order_by(DailyRun.id.desc()).limit(4):
                started = run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=datetime.UTC)
                if started.astimezone(tz).date() == today:
                    todays = run
                    break
            if todays is not None:
                finished = todays.finished_at
                if finished is not None and finished.tzinfo is None:
                    finished = finished.replace(tzinfo=datetime.UTC)
                out.update(
                    run=todays.status.value.lower(),
                    finished=finished.astimezone(tz).strftime("%H:%M") if finished else None,
                    new_jobs=todays.jobs_discovered, read=todays.jobs_evaluated, reason=todays.failure_reason,
                    recommendations=s.query(JobRecommendation).filter_by(daily_run_id=todays.id).count(),
                )
    except OperationalError:
        out["run"] = "busy"  # the daily run holds the database while it works
    except Exception as exc:  # noqa: BLE001
        logger.warning("alive message: could not read today's run: %s", type(exc).__name__)

    if out["run"] in ("unknown", "busy"):
        try:
            from naukri_agent.orchestration.watchdog import read_task_info

            info = (task_info_fn or read_task_info)(settings.watchdog_daily_task_name)
            if info is not None and (info.state or "").lower() == "running":
                out["run"] = "running"
            elif out["run"] == "unknown":
                out["run"] = "not_run"
        except Exception:  # noqa: BLE001
            pass
    try:
        if ready_fn is None:
            from naukri_agent.orchestration.telegram_listener import ready_summary as ready_fn
        out["ready"], out["remaining"], out["cap"] = ready_fn(settings, now)
    except Exception as exc:  # noqa: BLE001 - a busy database is common while the run works
        logger.info("alive message: could not read the ready jobs: %s", type(exc).__name__)
    return out


def build_alive_text(phone: str, status: dict[str, Any], settings: Settings, now_local: datetime.datetime) -> str:
    lines = [f"Daily check ({now_local.strftime('%a %d %b, %H:%M')}): the PC is on and the app is alive."]
    phone = phone.lower()  # a dry run reports Task Scheduler's own word, "Running"
    if phone in ("running", "started"):
        lines.append("Phone control is running, so /apply works.")
    elif phone in ("failed", "disabled"):
        lines.append("Phone control is NOT running and I could not start it, so /apply will not work. Please check the PC.")
    run = status.get("run")
    if run == "completed":
        n = status.get("recommendations")
        digest = f"{n} job(s) to apply yourself on company sites" if n else "nothing new to apply for yourself"
        lines.append(
            f"Job search: finished at {status.get('finished') or '?'} - {status.get('read')} jobs read, "
            f"{status.get('new_jobs')} new. Digest emailed: {digest}."
        )
    elif run == "failed":
        lines.append(f"Job search: FAILED today ({status.get('reason') or 'no reason recorded'}). Check the PC or re-run it.")
    elif run in ("running", "busy"):
        lines.append("Job search: still running; the digest comes when it finishes.")
    elif run == "not_run":
        lines.append("Job search: has not run today. Is it past its start time? The watchdog will say if it never starts.")
    ready = status.get("ready")
    if ready is not None:
        remaining, cap = status.get("remaining"), status.get("cap")
        if ready and remaining:
            lines.append(f"Telegram apply: {ready} job(s) ready; you can approve up to {remaining} more today. Send /apply.")
        elif ready:
            lines.append(f"Telegram apply: {ready} job(s) ready, but today's limit ({cap} per 24h) is used.")
        else:
            lines.append("Telegram apply: no job is ready right now.")
    if settings.apply_reminder_enabled:
        lines.append(f"I will remind you at {settings.apply_reminder_time} if jobs are still waiting.")
    return "\n".join(lines)


# --- the guard -----------------------------------------------------------------------------------------------------------------


def run_phone_guard(
    settings: Settings,
    *,
    now: datetime.datetime | None = None,
    ensure_fn: Callable[[Settings], str] | None = None,
    notify: Callable[[str], None] | None = None,
    status_fn: Callable[..., dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> GuardResult:
    now = now or datetime.datetime.now(datetime.UTC)
    epoch = now.timestamp()
    today = _local(settings, epoch).date().isoformat()
    result = GuardResult()

    if dry_run:
        result.phone_control = phone_control_status(settings.phone_control_task_name)
        result.notes.append("dry run: nothing was started or sent")
    else:
        result.phone_control = (ensure_fn or ensure_phone_control_running)(settings)

    state = _load_state(settings.phone_guard_state_file, today)

    def send(text: str) -> bool:
        nonlocal notify
        if dry_run:
            return False
        if notify is None:
            if not (settings.telegram_bot_token and settings.telegram_chat_id):
                result.notes.append("Telegram is not set up, so nothing was sent")
                return False
            from naukri_agent.orchestration.telegram_interaction import build_telegram_interaction

            notify = build_telegram_interaction(settings).notify
        try:
            notify(text)
            return True
        except Exception as exc:  # noqa: BLE001 - a message that cannot be sent is a note, never a crash
            result.notes.append(f"could not send a message ({type(exc).__name__})")
            return False

    # 1. phone control could not be started: say so once a day (starting it again is silent; the listener says "Phone control is on")
    if result.phone_control in ("failed", "disabled") and not state.get("down_alerted"):
        if send("Phone control (the /apply listener) is not running and could not be started, so /apply will not work. "
                f"On the PC, start the '{settings.phone_control_task_name}' task."):
            state["down_alerted"] = True
            result.down_alerted = True

    # 2. the daily "alive" message
    if alive_due(settings, epoch, state):
        status = (status_fn or gather_status)(settings, now)
        text = build_alive_text(result.phone_control, status, settings, _local(settings, epoch))
        if dry_run:
            result.preview = text
        elif send(text):
            state["alive_sent"] = True
            result.alive_sent = True
            logger.info("phone guard: the daily alive message was sent")

    if not dry_run:
        _save_state(settings.phone_guard_state_file, state)
    return result
