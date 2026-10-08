"""
Daily Naukri profile refresh by resume rotation.

    naukri-agent profile-refresh              # dry run: shows which resume is next, changes nothing
    naukri-agent profile-refresh --execute    # uploads it (needs PROFILE_REFRESH_ENABLED=true)

Uploading a resume is what makes Naukri treat the profile as freshly updated, which
recruiters' searches favour. The user has three resumes (config/resumes.yaml); this
uploads them one after another, one per day, in registry order. It only ever uploads
the user's own existing files - it never edits, generates or rewords anything.

Safety, in order:
  * off unless PROFILE_REFRESH_ENABLED=true AND --execute is given;
  * a pause file (data/PAUSE_PROFILE_REFRESH) stops it;
  * at most one successful upload per local calendar day;
  * a login CAPTCHA/MFA is never worked around - the day is recorded as needs_human;
  * an upload counts (and advances the rotation) only if the profile, reloaded, shows the
    new file; otherwise it is "unconfirmed" and tomorrow retries the same resume;
  * the file must exist and is hashed, so a changed file is visible in the record.

Note for the apply agent: Naukri applies with the profile's CURRENT resume, so whichever
resume was uploaded today is the one jobs applied to today will carry.
"""

from __future__ import annotations

import datetime
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from naukri_agent.config import Settings

logger = logging.getLogger(__name__)


class ProfileRefreshResult(BaseModel):
    # would_upload | uploaded | unconfirmed | failed | needs_human | disabled | paused | already_done | nothing_to_do
    outcome: str
    resume_id: str | None = None
    detail: str = ""
    before_filename: str | None = None
    after_filename: str | None = None


@contextmanager
def _open_naukri_client(settings: Settings) -> Iterator[Any]:
    from naukri_agent.browser.browser_manager import BrowserManager
    from naukri_agent.browser.naukri_client import NaukriClient

    with BrowserManager(settings) as browser:
        client = NaukriClient(browser.page, settings)
        client.login()
        yield client


def pick_next_resume(entries: list[Any], last_uploaded_id: str | None) -> Any | None:
    """The entry after the last uploaded one, wrapping round; the first if there is no history
    (or the last one is no longer in the registry)."""
    if not entries:
        return None
    ids = [e.id for e in entries]
    if last_uploaded_id in ids:
        return entries[(ids.index(last_uploaded_id) + 1) % len(entries)]
    return entries[0]


def _local_date(moment: datetime.datetime, tz_name: str) -> datetime.date:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=datetime.UTC)
    return aware.astimezone(ZoneInfo(tz_name)).date()


def run_profile_refresh(
    settings: Settings,
    *,
    execute: bool = False,
    session_factory: Any = None,
    open_client: Callable[[Settings], Any] | None = None,
    now: datetime.datetime | None = None,
    notify: Callable[[str], None] | None = None,
) -> ProfileRefreshResult:
    from naukri_agent.browser.exceptions import NaukriCaptchaError, NaukriMfaError
    from naukri_agent.database.base import init_db, session_scope
    from naukri_agent.database.repositories import add_resume_refresh, last_uploaded_resume
    from naukri_agent.resume.registry import compute_file_hash, load_resume_registry

    now = now or datetime.datetime.now(datetime.UTC)
    say = notify or (lambda text: None)

    if execute:
        if not settings.profile_refresh_enabled:
            return ProfileRefreshResult(outcome="disabled", detail="PROFILE_REFRESH_ENABLED is not true")
        if Path(settings.profile_refresh_pause_file).exists():
            return ProfileRefreshResult(outcome="paused", detail=f"pause file present: {settings.profile_refresh_pause_file}")

    registry = load_resume_registry(settings.resume_registry_path)
    factory = session_factory or init_db(settings)

    with session_scope(factory) as s:
        last = last_uploaded_resume(s)
        last_id = last.resume_id if last else None
        last_at = last.attempted_at if last else None

    if execute and last_at is not None and _local_date(last_at, settings.timezone) == _local_date(now, settings.timezone):
        return ProfileRefreshResult(outcome="already_done", resume_id=last_id, detail="already uploaded today")

    entry = pick_next_resume(registry.resumes, last_id)
    if entry is None:
        return ProfileRefreshResult(outcome="nothing_to_do", detail="no resumes in the registry")

    path = Path(entry.file)
    if not path.is_file():
        return ProfileRefreshResult(outcome="failed", resume_id=entry.id, detail=f"resume file not found: {path}")

    if not execute:
        return ProfileRefreshResult(outcome="would_upload", resume_id=entry.id, detail=f"would upload {path.name} (dry run)")

    file_hash = compute_file_hash(path)

    def record(outcome: str, detail: str = "") -> None:
        with session_scope(factory) as s:
            add_resume_refresh(s, resume_id=entry.id, outcome=outcome, file_hash=file_hash,
                               detail=detail[:500] or None, attempted_at=now)

    try:
        with (open_client or _open_naukri_client)(settings) as client:
            up = client.upload_resume(path)
    except (NaukriCaptchaError, NaukriMfaError) as exc:
        record("needs_human", type(exc).__name__)
        say(f"Profile refresh: Naukri asked for a CAPTCHA/OTP, so no resume was uploaded today ({entry.id}).")
        return ProfileRefreshResult(outcome="needs_human", resume_id=entry.id, detail=type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - any surprise: record the type only and stop
        name = type(exc).__name__
        record("failed", name)
        say(f"Profile refresh failed ({name}); nothing was changed on your profile.")
        return ProfileRefreshResult(outcome="failed", resume_id=entry.id, detail=name)

    if up.verified:
        record("uploaded", up.note or "")
        say(f"Profile refreshed: uploaded {entry.id} ({path.name}). Naukri now shows: {up.after_filename}.")
        return ProfileRefreshResult(
            outcome="uploaded", resume_id=entry.id, before_filename=up.before_filename, after_filename=up.after_filename
        )
    record("unconfirmed", up.note or "")
    say(f"Profile refresh: uploaded {entry.id} but Naukri did not show it afterwards - check your profile.")
    return ProfileRefreshResult(
        outcome="unconfirmed", resume_id=entry.id, detail=up.note or "",
        before_filename=up.before_filename, after_filename=up.after_filename,
    )
