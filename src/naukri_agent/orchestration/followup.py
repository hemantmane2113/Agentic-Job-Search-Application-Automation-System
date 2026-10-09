"""
"Did you apply?" for company-website jobs, asked on Telegram.

The app cannot see what you do on a company's own site, so until now such a job stayed unrecorded unless you ran
`mark-applied` yourself. The phone listener now asks, one job at a time, with three buttons:

    Applied        recorded as "applied through company website"; never suggested again
    Not applying   recorded as "not applied"; never suggested again
    Later          asked again next time; the 4th Later in a row records it as "ignored" and it is never suggested again

It asks when you send /applied, and once a day at settings.followup_reminder_time (20:00) when some job is still
waiting for an answer. It only asks about company-website jobs from recent digests that have no record yet.

Safety: it records only what you tap. No answer in time ends the questions and changes nothing. Never imports any apply
code and never touches Naukri.
"""

from __future__ import annotations

import datetime
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy.exc import OperationalError

from naukri_agent.config import Settings
from naukri_agent.notifications.telegram import TelegramError

logger = logging.getLogger(__name__)

OPTIONS = ["Applied", "Not applying", "Later"]
_ANSWER = {"Applied": "applied", "Not applying": "not_applying", "Later": "later"}
STOP_WORD = "/stop"


# --- the daily reminder -------------------------------------------------------------------------------------------------


def _local(settings: Settings, epoch: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(epoch, ZoneInfo(settings.timezone))


def _state_date(path: Path) -> str | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("last_reminder_date")
    except (OSError, ValueError, AttributeError):
        return None


def reminder_due(settings: Settings, epoch: float) -> bool:
    """True once the day's reminder time has passed and today's reminder has not been handled yet."""
    if not settings.followup_reminder_enabled:
        return False
    local = _local(settings, epoch)
    hour, minute = (int(p) for p in settings.followup_reminder_time.split(":"))
    if (local.hour, local.minute) < (hour, minute):
        return False
    return _state_date(settings.followup_state_file) != local.date().isoformat()


def mark_reminder_handled(settings: Settings, epoch: float) -> None:
    path = Path(settings.followup_state_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_reminder_date": _local(settings, epoch).date().isoformat()}), encoding="utf-8")
    except OSError as exc:  # worst case it asks again after a restart; never fatal
        logger.warning("could not save the reminder state: %s", type(exc).__name__)


# --- the questions ------------------------------------------------------------------------------------------------------


def job_card(index: int, total: int, job: dict, ignore_after: int) -> str:
    lines = [f"Did you apply?  ({index} of {total})", "", job["title"], job["company"] + (f" - {job['location']}" if job.get("location") else "")]
    if job.get("apply_redirect_url"):
        lines.append(f"Direct apply link: {job['apply_redirect_url']}")
    lines.append(f"Naukri: {job['url']}")
    if job.get("resume_id"):
        lines.append(f"Suggested resume: {job['resume_id']}")
    if job.get("later_count"):
        lines.append(f"You answered Later {job['later_count']} time(s) before. Answer Later {ignore_after} times in a row and it is ignored.")
    return "\n".join(lines)


def _confirmation(outcome: str, count: int, ignore_after: int) -> str:
    if outcome == "applied":
        return "Recorded: applied through company website."
    if outcome == "not_applying":
        return "Recorded: not applied. You will not see this job again."
    if outcome == "ignored":
        return f"Recorded: ignored. You said Later {count} times in a row, so you will not see this job again."
    if count == ignore_after - 1:
        return f"Okay, later. Last time I will ask: one more Later and I will ignore this job for good."
    return "Okay, I will ask again later."


class FollowupSession:
    def __init__(self, settings: Settings, channel: Any, *, factory: Any = None) -> None:
        self.settings, self.ch = settings, channel
        self._factory = factory

    def _db(self) -> Any:
        if self._factory is None:
            from naukri_agent.database.base import init_db

            self._factory = init_db(self.settings)
        return self._factory

    def _say(self, text: str) -> None:
        try:
            self.ch.send(text)
        except TelegramError:
            logger.warning("telegram reply failed")

    def run(self, *, quiet_if_empty: bool = False, now: datetime.datetime | None = None) -> str:
        """Ask about every waiting job. Returns "done", "empty" (nothing waiting) or "busy" (the database is in use)."""
        from naukri_agent.database.base import session_scope
        from naukri_agent.database.repositories import followup_candidates, record_followup_answer

        s = self.settings
        now = now or datetime.datetime.now(datetime.UTC)
        try:
            with session_scope(self._db()) as session:
                jobs = followup_candidates(session, now, s.followup_lookback_days, s.followup_max_jobs)
        except OperationalError:
            if not quiet_if_empty:
                self._say("The database is busy - the daily run is probably still going. Send /applied again when it has finished.")
            return "busy"
        if not jobs:
            if not quiet_if_empty:
                self._say("Nothing is waiting: every recent company-website job already has an answer.")
            return "empty"

        self._say(
            f"{len(jobs)} company-website job(s) are waiting for an answer. Tap a button for each one; "
            f"send {STOP_WORD} to stop. Nothing changes unless you tap."
        )
        counts = {"applied": 0, "not_applying": 0, "later": 0, "ignored": 0}
        for i, job in enumerate(jobs, start=1):
            try:
                reply = self.ch.ask_choice(
                    job_card(i, len(jobs), job, s.followup_ignore_after_later), OPTIONS, s.followup_answer_timeout_minutes * 60
                )
            except TelegramError as exc:  # the connection stayed down; nothing was recorded for this job
                logger.warning("follow-up questions stopped: %s", exc)
                break
            if reply is None:
                self._say("No answer, so I stopped here. Nothing was changed for the jobs I did not hear back on.")
                break
            if reply == STOP_WORD:
                self._say("Stopped. The jobs I did not ask about stay as they were.")
                break
            try:
                with session_scope(self._db()) as session:
                    outcome, count = record_followup_answer(
                        session, job["job_id"], _ANSWER[reply], ignore_after_later=s.followup_ignore_after_later
                    )
            except Exception as exc:  # noqa: BLE001 - reported by type only; never lose the rest silently
                logger.warning("could not record a follow-up answer: %s", type(exc).__name__)
                self._say(f"Could not save that answer ({type(exc).__name__}). I stopped here.")
                break
            counts[outcome] += 1
            self._say(_confirmation(outcome, count, s.followup_ignore_after_later))
        self._say(
            "Done. "
            f"Applied: {counts['applied']}, not applying: {counts['not_applying']}, "
            f"later: {counts['later']}, ignored: {counts['ignored']}."
        )
        return "done"


def run_followup(settings: Settings, channel: Any, *, quiet_if_empty: bool = False) -> str:
    return FollowupSession(settings, channel).run(quiet_if_empty=quiet_if_empty)
