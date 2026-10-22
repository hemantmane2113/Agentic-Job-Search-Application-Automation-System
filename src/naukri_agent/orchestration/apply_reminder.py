"""
The evening reminder: which Naukri Apply jobs are still waiting for you, and how long each stays on the list.

A job that scores 80 or more and has a Naukri Apply button stays "ready" for telegram-apply for a week, but nothing
used to tell you a job was waiting once the morning message had scrolled away, or when it would drop off. Once a day, at
`apply_reminder_time`, the phone listener sends a short list and tells you to send /apply when you are free. It is only
a message: it starts nothing and applies nothing. Silent when no job is waiting.

The pieces here are pure functions; the listener decides when to call them.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

from naukri_agent.config import Settings

logger = logging.getLogger(__name__)

_MAX_LISTED = 8


def _local(settings: Settings, epoch: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(epoch, ZoneInfo(settings.timezone))


def _hm(text: str) -> tuple[int, int]:
    hour, minute = (int(p) for p in text.split(":"))
    return hour, minute


def _state_date(path: Path) -> str | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("last_reminder_date")
    except (OSError, ValueError, AttributeError):
        return None


def apply_reminder_due(settings: Settings, epoch: float) -> bool:
    """True once the reminder time has passed, today's has not been handled, and it is not too late in the evening to
    be useful (after the listener's awake window ends the PC is about to sleep)."""
    if not settings.apply_reminder_enabled:
        return False
    local = _local(settings, epoch)
    hm = (local.hour, local.minute)
    if hm < _hm(settings.apply_reminder_time) or hm >= _hm(settings.telegram_listener_awake_until):
        return False
    return _state_date(settings.apply_reminder_state_file) != local.date().isoformat()


def mark_apply_reminder_handled(settings: Settings, epoch: float) -> None:
    path = Path(settings.apply_reminder_state_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_reminder_date": _local(settings, epoch).date().isoformat()}), encoding="utf-8")
    except OSError as exc:  # worst case it is sent again after a restart; never fatal
        logger.warning("could not save the apply reminder state: %s", type(exc).__name__)


def _until(expires: datetime.datetime | None) -> str:
    return f"on the list until {expires.day} {expires.strftime('%b')}" if expires else "on the list"


def reminder_text(jobs: list[dict], remaining: int, cap: int, remote_start: bool = True) -> str | None:
    """The message, or None when nothing is waiting."""
    if not jobs:
        return None
    n = len(jobs)
    lines = [f"Evening reminder: {n} job{'s' if n != 1 else ''} waiting for Telegram apply.", ""]
    for i, j in enumerate(jobs[:_MAX_LISTED], 1):
        lines.append(f"{i}. {j['title']} - {j['company']}  (score {j['score']:.0f}, {_until(j.get('expires'))})")
    if n > _MAX_LISTED:
        lines.append(f"... and {n - _MAX_LISTED} more")
    lines.append("")
    if remaining > 0:
        lines.append(f"You can approve up to {remaining} more today (limit {cap} per 24h).")
        lines.append(
            ("Send /apply to start" if remote_start else "Run naukri-agent telegram-apply to start")
            + "; nothing is applied until you tap Yes on each job."
        )
    else:
        lines.append(f"The daily limit ({cap} per 24h) is already used, so these are offered again after it frees up.")
    return "\n".join(lines)
