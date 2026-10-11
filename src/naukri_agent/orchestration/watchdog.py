"""
Morning watchdog: tells the phone when the day's chain did not happen.

    naukri-agent watchdog            # evaluates what is due now; alerts on Telegram
    naukri-agent watchdog --dry-run  # shows what it would say; sends and records nothing

Run by a scheduled task (10:30 and 15:00). It checks three things and speaks only about PROBLEMS,
each at most once per day:
  * the profile refresh (09:45): was a resume uploaded today, or did it end in CAPTCHA / failure?
  * the job-search run (10:00): has it started?  Windows Task Scheduler is asked directly, because
    the run saves its database record only when it FINISHES (hours later), so the database cannot
    say whether it is under way.
  * the digest: by 15:00 is there a finished run for today, or is it failed / stuck / killed?

Limits, plainly: it runs on the same PC, so a PC that is switched off cannot alert anyone; and it
reads the main database without writing to it (the daily run holds that database for hours), keeping
its own once-a-day memory in a small JSON file instead.
"""

from __future__ import annotations

import datetime
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from naukri_agent.config import Settings

logger = logging.getLogger(__name__)


class TaskInfo(BaseModel):
    state: str | None = None  # Ready | Running | Disabled ...
    last_run: datetime.datetime | None = None  # aware, UTC; None = never ran
    last_result: int | None = None


class Problem(BaseModel):
    kind: str
    text: str


class WatchdogResult(BaseModel):
    problems: list[Problem] = Field(default_factory=list)
    alerted: list[str] = Field(default_factory=list)  # kinds actually sent this call
    already_alerted: list[str] = Field(default_factory=list)  # still true, but said earlier today
    notes: list[str] = Field(default_factory=list)  # things it could not check
    ok_message_sent: bool = False


_PS_QUERY = (
    "$t = Get-ScheduledTask -TaskName '__NAME__' -ErrorAction Stop; "
    "$i = Get-ScheduledTaskInfo -TaskName '__NAME__' -ErrorAction Stop; "
    "$r = $null; if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { $r = $i.LastRunTime.ToUniversalTime().ToString('o') }; "
    "[pscustomobject]@{State = [string]$t.State; LastRun = $r; LastResult = [int64]$i.LastTaskResult} | ConvertTo-Json -Compress"
)


def read_task_info(name: str) -> TaskInfo | None:
    """Ask Windows Task Scheduler about one task. None when it cannot be read (not Windows, no such task)."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_QUERY.replace("__NAME__", name.replace("'", "''"))],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        data = json.loads(out)
        last = data.get("LastRun")
        return TaskInfo(
            state=data.get("State"),
            last_run=datetime.datetime.fromisoformat(last.replace("Z", "+00:00")) if last else None,
            last_result=data.get("LastResult"),
        )
    except Exception as exc:  # noqa: BLE001 - "cannot read" is a note, never a crash
        logger.warning("could not read scheduled task %r: %s", name, type(exc).__name__)
        return None


def _hhmm(text: str) -> datetime.time:
    h, m = text.split(":")
    return datetime.time(int(h), int(m))


def _local(moment: datetime.datetime, tz: ZoneInfo) -> datetime.datetime:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=datetime.UTC)
    return aware.astimezone(tz)


def _result_text(info: TaskInfo) -> str:
    if info.last_result is None:
        return "unknown"
    return "success" if info.last_result == 0 else f"0x{info.last_result & 0xFFFFFFFF:X}"


def _load_state(path: Path) -> dict[str, list[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing/corrupt memory just means "nothing said yet"
        return {}


def _save_state(path: Path, state: dict[str, list[str]], today: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({today: state.get(today, [])}), encoding="utf-8")  # keep only today
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not save watchdog memory: %s", type(exc).__name__)


def evaluate(
    settings: Settings,
    *,
    now: datetime.datetime,
    task_info: TaskInfo | None,
    refreshes_today: list[tuple[str, str]],  # (resume_id, outcome) of today's refresh attempts, oldest first
    runs_today: list[tuple[str, str | None]],  # (status, failure_reason) of today's FINISHED runs
    db_readable: bool = True,
) -> tuple[list[Problem], list[str]]:
    """Pure decision logic: what is wrong right now, given what can be observed. Returns (problems, notes)."""
    tz = ZoneInfo(settings.timezone)
    local_now = _local(now, tz)
    tod = local_now.time()
    start_by, digest_by = _hhmm(settings.watchdog_run_start_by), _hhmm(settings.watchdog_digest_by)
    problems: list[Problem] = []
    notes: list[str] = []

    # 1. profile refresh
    if tod >= start_by and settings.profile_refresh_enabled and not Path(settings.profile_refresh_pause_file).exists():
        if not db_readable:
            notes.append("profile refresh not checked: the database was busy")
        elif not refreshes_today:
            problems.append(Problem(kind="refresh_missing", text="No profile refresh was recorded today (it is due at 09:45)."))
        elif not any(outcome == "uploaded" for _rid, outcome in refreshes_today):
            rid, outcome = refreshes_today[-1]
            hint = {
                "needs_human": "Naukri asked for a CAPTCHA/OTP; log in once yourself.",
                "unconfirmed": "the upload was not visible on the profile afterwards; please check it.",
                "failed": "the upload failed; the profile was not changed.",
            }.get(outcome, "")
            problems.append(Problem(kind=f"refresh_{outcome}", text=f"Today's profile refresh ended as '{outcome}' ({rid}); {hint}".rstrip("; ")))

    # 2. has the 10:00 run started?
    started_today = False
    if task_info is None:
        notes.append("job-search task not checked: Task Scheduler could not be read")
    else:
        running = (task_info.state or "").lower() == "running"
        last_local = _local(task_info.last_run, tz) if task_info.last_run else None
        started_today = running or (last_local is not None and last_local.date() == local_now.date())
        if tod >= start_by and not started_today:
            when = last_local.strftime("%d %b %H:%M") if last_local else "never"
            problems.append(Problem(
                kind="run_not_started",
                text=(f"Today's job-search run has not started (task state {task_info.state}, last run {when}, "
                      f"last result {_result_text(task_info)}). Is the PC on and signed in?"),
            ))

    # 3. is there a digest by the cut-off?
    if tod >= digest_by:
        failed = [r for r in runs_today if r[0] == "FAILED"]
        done = [r for r in runs_today if r[0] == "COMPLETED"]
        running = task_info is not None and (task_info.state or "").lower() == "running"
        if not db_readable and not running:
            notes.append("digest not checked: the database was busy")
        elif done:
            pass
        elif failed:
            problems.append(Problem(kind="run_failed", text=f"Today's run failed: {failed[-1][1] or 'no reason recorded'}."))
        elif running and task_info and task_info.last_run:
            hours = (now - (task_info.last_run if task_info.last_run.tzinfo else task_info.last_run.replace(tzinfo=datetime.UTC))).total_seconds() / 3600
            if hours >= settings.watchdog_max_run_hours:
                problems.append(Problem(
                    kind="run_long",
                    text=f"Today's run has been going for {hours:.1f} hours (normally about 3); it may be stuck. Windows stops it at 6 hours.",
                ))
        elif task_info is not None and started_today:
            problems.append(Problem(
                kind="run_no_digest",
                text=f"The run is no longer running but produced no digest (last result {_result_text(task_info)}); it was probably stopped or killed. No email was sent.",
            ))
        # not started at all is already reported by check 2

    return problems, notes


def _read_db(settings: Settings, session_factory: Any, now: datetime.datetime) -> tuple[list, list, bool]:
    """Today's refresh attempts and finished runs, read-only. (refreshes, runs, readable)."""
    from sqlalchemy.exc import OperationalError

    from naukri_agent.database.base import init_db, session_scope
    from naukri_agent.database.models import DailyRun, ResumeRefresh

    tz = ZoneInfo(settings.timezone)
    today = _local(now, tz).date()
    try:
        factory = session_factory or init_db(settings)
        with session_scope(factory) as s:
            refreshes = [
                (r.resume_id, r.outcome)
                for r in s.query(ResumeRefresh).order_by(ResumeRefresh.id)
                if _local(r.attempted_at, tz).date() == today
            ]
            runs = [
                (r.status.value if hasattr(r.status, "value") else str(r.status), r.failure_reason)
                for r in s.query(DailyRun).order_by(DailyRun.id)
                if r.started_at is not None and _local(r.started_at, tz).date() == today
            ]
        return refreshes, runs, True
    except OperationalError:
        return [], [], False


def run_watchdog(
    settings: Settings,
    *,
    now: datetime.datetime | None = None,
    task_info_fn: Callable[[str], TaskInfo | None] | None = None,
    session_factory: Any = None,
    notify: Callable[[str], None] | None = None,
    dry_run: bool = False,
) -> WatchdogResult:
    now = now or datetime.datetime.now(datetime.UTC)
    tz = ZoneInfo(settings.timezone)
    today = _local(now, tz).date().isoformat()

    refreshes, runs, readable = _read_db(settings, session_factory, now)
    info = (task_info_fn or read_task_info)(settings.watchdog_daily_task_name)
    problems, notes = evaluate(settings, now=now, task_info=info, refreshes_today=refreshes, runs_today=runs, db_readable=readable)
    result = WatchdogResult(problems=problems, notes=notes)

    state = _load_state(Path(settings.watchdog_state_file))
    said = state.setdefault(today, [])
    fresh = [p for p in problems if p.kind not in said]
    result.already_alerted = [p.kind for p in problems if p.kind in said]
    if dry_run:
        return result

    if fresh:
        if notify is None and settings.telegram_bot_token and settings.telegram_chat_id:
            from naukri_agent.orchestration.telegram_interaction import build_telegram_interaction

            notify = build_telegram_interaction(settings).notify
        if notify is None:
            result.notes.append("Telegram is not set up, so nothing could be sent")
        else:
            text = "Naukri watchdog:\n" + "\n".join(f"- {p.text}" for p in fresh)
            try:
                notify(text)
                said.extend(p.kind for p in fresh)
                result.alerted = [p.kind for p in fresh]
            except Exception as exc:  # noqa: BLE001 - a failed alert is reported, not raised
                result.notes.append(f"could not send the alert ({type(exc).__name__})")
    elif not problems and settings.watchdog_send_ok and (
        notify is not None or (settings.telegram_bot_token and settings.telegram_chat_id)
    ):
        local_tod = _local(now, tz).time()
        if local_tod >= _hhmm(settings.watchdog_digest_by) and "ok" not in said:
            if notify is None:
                from naukri_agent.orchestration.telegram_interaction import build_telegram_interaction

                notify = build_telegram_interaction(settings).notify
            try:
                notify("Naukri watchdog: all good today - profile refreshed and the digest was sent.")
                said.append("ok")
                result.ok_message_sent = True
            except Exception as exc:  # noqa: BLE001
                result.notes.append(f"could not send the all-good message ({type(exc).__name__})")

    _save_state(Path(settings.watchdog_state_file), state, today)
    return result
