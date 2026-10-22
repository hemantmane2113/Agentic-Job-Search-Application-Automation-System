"""
Employment-type filter: keep full-time, permanent jobs; leave out contract, temporary, freelance,
part-time and internship jobs.

The decision rests on Naukri's OWN "Employment Type" field (for example "Full Time, Permanent"), read
from the job page. It deliberately does not use the AI's job_type guess: on a sample of 8 jobs the AI
called "contract", 6 were "Full Time, Permanent" on Naukri, so filtering on it would have hidden good
jobs. A job whose page shows no Employment Type is NOT removed; it is kept and marked "not confirmed".

When the page shows no type, a title such as 'Data Scientist (Freelancer)' or '... Intern' is also caught.
Plain rules, no AI. This is an eligibility gate on what gets recommended, offered for applying or
researched. It is not part of scoring.
"""

from __future__ import annotations

import re

OK = "ok"  # the page says full time (and/or permanent)
EXCLUDED = "excluded"  # contract / temporary / freelance / part time / internship
UNCONFIRMED = "unconfirmed"  # no Employment Type on the page: kept, but marked

_EXCLUDE = re.compile(r"contract|temporar|freelanc|part[\s\-]*time|\bintern(?:ship)?\b|\bvolunteer|apprentice", re.I)
_FULL = re.compile(r"full[\s\-]*time|permanent", re.I)


def classify_employment(text: str | None) -> str:
    """OK / EXCLUDED / UNCONFIRMED for one Employment Type string (case-insensitive)."""
    if not text or not text.strip():
        return UNCONFIRMED
    if _EXCLUDE.search(text):
        return EXCLUDED
    if _FULL.search(text):
        return OK
    return UNCONFIRMED


def is_excluded(text: str | None, enabled: bool = True, title: str | None = None) -> bool:
    """True when the job should be left out. Naukri's Employment Type decides whenever the page shows one.
    Only when it shows none does the job TITLE count, so '... (Freelancer)' or '... Intern' is still caught."""
    if not enabled:
        return False
    kind = classify_employment(text)
    if kind == EXCLUDED:
        return True
    return kind == UNCONFIRMED and bool(title) and bool(_EXCLUDE.search(title))


def describe(text: str | None) -> str:
    """A short phrase for messages."""
    kind = classify_employment(text)
    if kind == UNCONFIRMED:
        return "not confirmed"
    return (text or "").strip()[:40]
