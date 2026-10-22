"""
Start `telegram-apply` from your phone.

`naukri-agent telegram-listen` runs on the PC all day. It waits for a message from YOUR Telegram chat:

    /apply    start `telegram-apply` now (the job cards then arrive on the phone as usual)
    /status   how many jobs are ready and how many you can still approve today
    /help     the list of commands

What it will and will not do:
  * It only hears the ONE configured chat; anyone else is ignored (the channel enforces it).
  * It never applies by itself. It starts the existing `telegram-apply` command, which still sends
    every job to the phone and clicks nothing until you tap Yes. Silence still means no.
  * `telegram-apply` needs AUTO_APPLY=true and DRY_RUN=false. The listener does not change its own
    settings; it sets those two for the one process it starts, and only because you sent /apply.
  * It is off unless TELEGRAM_REMOTE_START=true (see the `telegram-listen` command).
  * An old "/apply" (sent while the PC was asleep or off) is not acted on: only messages younger than
    telegram_listener_max_command_age_seconds are. You are told so, and asked to send it again.
  * Between telegram_listener_awake_from and _until it keeps the PC awake (power.AwakeWindow), because a sleeping PC
    cannot hear Telegram; and once a day, at apply_reminder_time, it lists the jobs still waiting (apply_reminder.py).
  * Every message it hears, and every one it ignores, is written to the log (the command word only, never the text),
    with a "still listening" line every half hour and a note when it was not listening for a while.
  * It starts the command as a separate process and never imports any apply code (a test guards
    that). While that process runs, the listener does not read Telegram, because the apply run is
    reading your taps.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import subprocess
import sys
import time
from typing import Any, Callable

from zoneinfo import ZoneInfo

from naukri_agent.browser.profile_lock import holder_if_busy
from naukri_agent.config import Settings
from naukri_agent.notifications.telegram import TelegramChannel, TelegramError
from naukri_agent.orchestration.apply_lock import apply_lock_held
from naukri_agent.orchestration.apply_reminder import (
    apply_reminder_due,
    mark_apply_reminder_handled,
    reminder_text,
)
from naukri_agent.orchestration.followup import mark_reminder_handled, reminder_due, run_followup
from naukri_agent.power import AwakeWindow

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "Phone control is on.\n"
    "/apply  - start applying (a card per job arrives; nothing is applied until you tap Yes)\n"
    "/status - how many jobs are ready, and how many you can still approve today\n"
    "/applied - tell me which company-website jobs you applied to (also asked every evening)\n"
    "/help   - this list"
)
_BACKOFF_SECONDS = (10, 30, 60)
_LOCKED_WAIT_SECONDS = 5
_GAP_NOTE_SECONDS = 240  # a longer silence than this between two rounds of listening is worth a line in the log
_HEARTBEAT_SECONDS = 1800


def _human_age(seconds: float) -> str:
    minutes = max(1, int(seconds // 60))
    if minutes < 90:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = round(seconds / 3600)
    return f"{hours} hour{'s' if hours != 1 else ''}"


def _command_of(text: str) -> str:
    """'/apply@MyBot extra' -> '/apply'"""
    first = (text.strip().split() or [""])[0].lower()
    return first.split("@", 1)[0]


def spawn_telegram_apply(settings: Settings) -> tuple[int, str]:
    """Run `telegram-apply` as its own process with the two switches it needs. Returns (exit code, output).
    The switches are set for that process only; this process's own settings are untouched."""
    env = {**os.environ, "AUTO_APPLY": "true", "DRY_RUN": "false", "PYTHONIOENCODING": "utf-8"}
    cmd = [sys.executable, "-c", "from naukri_agent.cli.main import main; main()", "telegram-apply"]
    try:
        done = subprocess.run(
            cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=settings.telegram_listener_run_timeout_minutes * 60,
        )
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    return done.returncode, (done.stdout or "") + "\n" + (done.stderr or "")


def ready_summary(settings: Settings, now: datetime.datetime) -> tuple[int, int, int]:
    """(jobs ready, approvals left today, daily cap). Read-only. Raises sqlalchemy OperationalError when the
    database is locked, which is how a running daily run shows up."""
    from naukri_agent.candidate.models import load_candidate_profile
    from naukri_agent.database.base import init_db, session_scope
    from naukri_agent.database.models import Candidate
    from naukri_agent.database.repositories import auto_apply_count_since
    from naukri_agent.recommendations.apply_ready import select_candidates

    factory = init_db(settings)
    profile = load_candidate_profile(settings.candidate_profile_path)
    with session_scope(factory) as session:
        cand = session.query(Candidate).filter_by(email=profile.email).one_or_none()
        if cand is None:
            return 0, settings.auto_apply_daily_cap, settings.auto_apply_daily_cap
        ready = len(select_candidates(session, cand.id, settings, now))
        remaining = max(
            0, settings.auto_apply_daily_cap - auto_apply_count_since(session, now - datetime.timedelta(hours=24))
        )
    return ready, remaining, settings.auto_apply_daily_cap


def waiting_summary(settings: Settings, now: datetime.datetime) -> tuple[list[dict], int, int]:
    """(jobs waiting with the date each stays on the list, approvals left today, daily cap). Read-only. Raises sqlalchemy
    OperationalError when the database is locked."""
    from naukri_agent.candidate.models import load_candidate_profile
    from naukri_agent.database.base import init_db, session_scope
    from naukri_agent.database.models import Candidate
    from naukri_agent.database.repositories import auto_apply_count_since
    from naukri_agent.recommendations.apply_ready import waiting_jobs

    factory = init_db(settings)
    profile = load_candidate_profile(settings.candidate_profile_path)
    with session_scope(factory) as session:
        cand = session.query(Candidate).filter_by(email=profile.email).one_or_none()
        if cand is None:
            return [], settings.auto_apply_daily_cap, settings.auto_apply_daily_cap
        jobs = waiting_jobs(session, cand.id, settings, now)
        remaining = max(
            0, settings.auto_apply_daily_cap - auto_apply_count_since(session, now - datetime.timedelta(hours=24))
        )
    return jobs, remaining, settings.auto_apply_daily_cap


def _error_line(output: str) -> str | None:
    """The 'Error: ...' line the command prints for a plain, expected problem (database busy, switches off)."""
    for line in reversed([ln.strip() for ln in output.splitlines() if ln.strip()]):
        if line.startswith("Error:"):
            return line[:200]
    return None


class Listener:
    def __init__(
        self,
        settings: Settings,
        channel: Any,
        *,
        spawn: Callable[[Settings], tuple[int, str]] = spawn_telegram_apply,
        summary: Callable[[Settings, datetime.datetime], tuple[int, int, int]] = ready_summary,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        lock_held: Callable[[Any], bool] = apply_lock_held,
        followup: Callable[..., str] = run_followup,
        waiting: Callable[[Settings, datetime.datetime], tuple[list[dict], int, int]] = waiting_summary,
        browser_busy: Callable[[Any], str | None] = holder_if_busy,
        awake: AwakeWindow | None = None,
    ) -> None:
        self.settings, self.ch = settings, channel
        self._spawn, self._summary, self._clock, self._sleep, self._lock_held = spawn, summary, clock, sleep, lock_held
        self._followup = followup
        self._waiting, self._browser_busy = waiting, browser_busy
        self._reminder_retry_at = 0.0  # when the database was busy at reminder time: try again after this
        self._apply_reminder_retry_at = 0.0
        if awake is None and settings.telegram_listener_keep_awake:
            awake = AwakeWindow(settings.telegram_listener_awake_from, settings.telegram_listener_awake_until)
        self._awake = awake
        self._last_tick: float | None = None
        self._last_heartbeat = 0.0

    # --- one command ---------------------------------------------------------------------------

    def _say(self, text: str) -> None:
        try:
            self.ch.send(text)
        except TelegramError:
            logger.warning("telegram reply failed")

    def _ready(self) -> tuple[int, int, int] | str:
        """The summary, or a sentence saying why it is not available."""
        from sqlalchemy.exc import OperationalError

        try:
            return self._summary(self.settings, datetime.datetime.fromtimestamp(self._clock(), datetime.UTC))
        except OperationalError:
            return "The database is busy - the daily run is probably still going. Send /apply again when it has finished."
        except Exception as exc:  # noqa: BLE001 - reported by type only
            logger.warning("could not read the ready jobs: %s", type(exc).__name__)
            return f"Could not check what is ready ({type(exc).__name__})."

    def handle(self, text: str) -> None:
        command = _command_of(text)
        logger.info("telegram command received: %s", command[:20] or "(text)")  # the command word only, nothing else
        if command in ("/help", "/start"):
            self._say(HELP_TEXT)
        elif command == "/status":
            got = self._ready()
            if isinstance(got, str):
                self._say(got)
                return
            ready, remaining, cap = got
            self._say(
                f"{ready} job(s) ready for Telegram apply. You can approve up to {remaining} more today "
                f"(limit {cap} per 24h)." + (" Send /apply to start." if ready and remaining else "")
            )
        elif command == "/apply":
            self._apply()
        elif command == "/applied":
            self._followup(self.settings, self.ch)
        elif command.startswith("/"):
            self._say("I do not know that command.\n" + HELP_TEXT)

    def _apply(self) -> None:
        got = self._ready()
        if isinstance(got, str):
            self._say(got)
            return
        ready, remaining, cap = got
        if remaining <= 0:
            self._say(f"The daily limit ({cap} per 24h) is already used, so nothing can be applied now.")
            return
        if ready <= 0:
            self._say("No job is ready to apply right now.")
            return
        busy = self._browser_busy(self.settings.browser_lock_file) if self.settings.browser_lock_enabled else None
        if busy:
            self._say(f"The PC is busy with {busy}, which uses the same browser. Send /apply again in a few minutes.")
            return
        offered = min(ready, remaining)
        self._say(
            f"Starting. {offered} job(s) will be offered, one card at a time. "
            "The first one should arrive within a minute. Nothing is applied until you tap Yes."
        )
        code, output = self._spawn(self.settings)
        if code == 0:
            return  # the run already told you how it went
        if code == -1:
            self._say("The apply run took too long and was stopped. Check the PC.")
        elif code == 2:
            m = re.search(r'"blocked_reason":\s*"([^"]{1,200})"', output)
            self._say("Could not start: " + (m.group(1) if m else "the apply run is switched off or paused on the PC."))
        else:
            line = _error_line(output)
            self._say(
                f"The apply run stopped (exit code {code}). " + (line if line else "See the messages above, or check the PC.")
            )

    # --- the loop ------------------------------------------------------------------------------

    def step(self) -> None:
        """One round of listening. A run started by hand holds the lock; while it does, Telegram is left to it."""
        if self._lock_held(self.settings.telegram_apply_lock_file):
            self._sleep(_LOCKED_WAIT_SECONDS)
            return
        self._maybe_remind()
        self._maybe_apply_reminder()
        told = False
        for text, sent_at in self.ch.poll_commands(25):
            age = None if sent_at is None else self._clock() - sent_at
            if age is not None and age > self.settings.telegram_listener_max_command_age_seconds:
                command = _command_of(text)
                logger.info(
                    "telegram message ignored: %s was sent %s ago (limit %d min)",
                    command[:20] or "(text)", _human_age(age), self.settings.telegram_listener_max_command_age_seconds // 60,
                )
                if command == "/apply" and not told:
                    told = True
                    self._say(
                        f"That /apply was sent {_human_age(age)} ago, while the PC was asleep or off, so I did not act on it. "
                        "The PC is awake now: send /apply again and the first job card should arrive within a minute."
                    )
                continue
            self.handle(text)
        self._last_tick = self._clock()  # a long apply run or question session above is not "silence"

    def _maybe_apply_reminder(self) -> None:
        """Once a day, at apply_reminder_time: list the Naukri Apply jobs still waiting and when each leaves the list.
        Silent when none is waiting. If the database is busy it tries again in 10 minutes."""
        from sqlalchemy.exc import OperationalError

        now = self._clock()
        if now < self._apply_reminder_retry_at or not apply_reminder_due(self.settings, now):
            return
        try:
            jobs, remaining, cap = self._waiting(self.settings, datetime.datetime.fromtimestamp(now, datetime.UTC))
        except OperationalError:
            self._apply_reminder_retry_at = now + 600
            return
        except Exception as exc:  # noqa: BLE001 - a reminder must never stop phone control; reported by type only
            logger.warning("could not build the apply reminder: %s", type(exc).__name__)
            mark_apply_reminder_handled(self.settings, now)
            return
        mark_apply_reminder_handled(self.settings, now)
        text = reminder_text(jobs, remaining, cap, self.settings.telegram_remote_start)
        if text:
            logger.info("apply reminder sent (%d job(s) waiting)", len(jobs))
            self._say(text)

    def _watch_the_clock(self) -> None:
        """Keep the PC awake inside the window, note long silences, and say now and then that we are still listening."""
        now = self._clock()
        if self._awake is not None:
            local = datetime.datetime.fromtimestamp(now, ZoneInfo(self.settings.timezone)).time()
            self._awake.update(local)
        if self._last_tick is not None and now - self._last_tick > _GAP_NOTE_SECONDS:
            logger.info(
                "telegram listener: nothing was heard for %s (the PC was asleep or off, or it was busy); "
                "messages sent meanwhile are read now", _human_age(now - self._last_tick),
            )
        self._last_tick = now
        if now - self._last_heartbeat >= _HEARTBEAT_SECONDS:
            self._last_heartbeat = now
            logger.info("telegram listener: listening (PC kept awake: %s)", "yes" if self._awake and self._awake.held else "no")

    def _maybe_remind(self) -> None:
        """Once a day, at the reminder time: ask about company-website jobs still waiting for an answer (silent when
        there are none). If the database is busy it tries again in 10 minutes."""
        now = self._clock()
        if now < self._reminder_retry_at or not reminder_due(self.settings, now):
            return
        status = self._followup(self.settings, self.ch, quiet_if_empty=True)
        if status == "busy":
            self._reminder_retry_at = now + 600
        else:
            mark_reminder_handled(self.settings, now)

    def run(self, should_stop: Callable[[], bool] = lambda: False) -> None:
        self._say(HELP_TEXT)
        failures = 0
        try:
            self._loop(should_stop, failures)
        finally:
            if self._awake is not None:
                self._awake.release()

    def _loop(self, should_stop: Callable[[], bool], failures: int) -> None:
        while not should_stop():
            try:
                self._watch_the_clock()
                self.step()
                failures = 0
            except TelegramError as exc:
                logger.warning("telegram listener: %s", exc)
                self._sleep(_BACKOFF_SECONDS[min(failures, len(_BACKOFF_SECONDS) - 1)])
                failures += 1
            except Exception as exc:  # noqa: BLE001 - one bad command must not end phone control; reported by type only
                logger.warning("telegram listener: a command failed (%s)", type(exc).__name__)
                self._sleep(_BACKOFF_SECONDS[min(failures, len(_BACKOFF_SECONDS) - 1)])
                failures += 1


def run_listener(settings: Settings, channel: Any = None, **kwargs: Any) -> None:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set in .env")
    should_stop = kwargs.pop("should_stop", lambda: False)
    ch = channel or TelegramChannel(settings.telegram_bot_token, settings.telegram_chat_id)
    Listener(settings, ch, **kwargs).run(should_stop)
