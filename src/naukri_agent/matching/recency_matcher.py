"""
Recency: a job posted today earns the full marks, a job posted a week ago earns the least.

    posted today -> 7 of 7        1 day -> 6      2 days -> 5     3 days -> 4
    4 days -> 3                   5 days -> 2     6 or 7 days -> 1
    older than 7 days, or no readable date -> 0

The marks scale with settings.weight_recency (7 by default). This only rewards timeliness; it says
nothing about how well a job suits the candidate, so it is deliberately a small share of the total.
Plain code, no AI.
"""

from __future__ import annotations

import datetime
import re

from naukri_agent.config import Settings
from naukri_agent.database.models import Job
from naukri_agent.matching.models import CategoryScore

_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_REL = re.compile(r"(\d+)\s*\+?\s*(minute|min|hour|hr|day|week|month|year)s?\s*ago", re.I)
_ZERO_WORDS = ("just now", "today", "few hours", "few minutes", "moments ago", "few seconds")
_MAX_AGE_DAYS = 7
_FULL_MARKS_DAYS = 7  # a job posted today is worth this many "steps"; each day back is one step less


def posted_age_days(posted_text: str | None, now: datetime.datetime) -> int | None:
    """Whole days since the job was posted, or None when the text cannot be read. Never negative."""
    if not posted_text or not posted_text.strip():
        return None
    text = posted_text.strip()
    iso = _ISO.search(text)
    if iso:
        try:
            posted = datetime.date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
        return max(0, (now.date() - posted).days)
    low = re.sub(r"\s+", " ", text.lower())
    if any(w in low for w in _ZERO_WORDS):
        return 0
    if low in ("yesterday", "a day ago", "one day ago"):
        return 1
    rel = _REL.search(low)
    if not rel:
        return None
    n, unit = int(rel.group(1)), rel.group(2)
    if unit in ("minute", "min", "hour", "hr"):
        return 0
    return n * {"day": 1, "week": 7, "month": 30, "year": 365}[unit]


def recency_steps(age_days: int | None) -> int:
    """7 for today ... 1 for 6-7 days; 0 when older than a week or unknown."""
    if age_days is None or age_days > _MAX_AGE_DAYS:
        return 0
    return max(1, _FULL_MARKS_DAYS - age_days)


def score_recency(job: Job, settings: Settings, now: datetime.datetime | None = None) -> CategoryScore:
    max_points = settings.weight_recency
    now = now or datetime.datetime.now(datetime.UTC)
    age = posted_age_days(job.posted_date_text, now)
    steps = recency_steps(age)
    points = max_points * steps / _FULL_MARKS_DAYS
    if age is None:
        return CategoryScore(points=0, max_points=max_points, negative_factors=["Posted date could not be read"])
    if steps == 0:
        return CategoryScore(points=0, max_points=max_points, negative_factors=[f"Posted {age} days ago"])
    when = "today" if age == 0 else ("1 day ago" if age == 1 else f"{age} days ago")
    return CategoryScore(points=points, max_points=max_points, positive_factors=[f"Posted {when}"])
