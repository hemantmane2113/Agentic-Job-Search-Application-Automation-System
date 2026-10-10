"""
Which application system does a careers page use, and so what will applying be like?

Plain code, no AI: it fetches the page (with the same guards as the agent's own fetch) and looks for the
names and addresses of well-known application systems. Measured on 12 real company-site jobs: about a
quarter use a standard form, about 40% need an account login or go through LinkedIn or email, the rest
are the company's own pages.

This only describes what applying will involve, so you can choose which jobs are worth doing. Nothing
here applies, logs in, or fills anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

# (name, pattern over the final address and the page HTML, level)
_SYSTEMS = [
    ("Greenhouse", r"greenhouse\.io", "easy"),
    ("Lever", r"lever\.co", "easy"),
    ("Ashby", r"ashbyhq\.com", "easy"),
    ("Workable", r"workable\.com", "easy"),
    ("SmartRecruiters", r"smartrecruiters\.com", "easy"),
    ("Recruitee", r"recruitee\.com", "easy"),
    ("Teamtailor", r"teamtailor\.com", "easy"),
    ("BambooHR", r"bamboohr\.com", "easy"),
    ("Freshteam", r"freshteam\.com", "medium"),
    ("Jobvite", r"jobvite\.com", "medium"),
    ("Zoho Recruit", r"zohorecruit\.|zoho\.com/recruit|zrecruit", "medium"),
    ("Keka", r"keka\.com|kekahire", "medium"),
    ("Darwinbox", r"darwinbox\.", "medium"),
    ("Workday", r"myworkdayjobs\.com|\.workday\.com", "account"),
    ("Taleo", r"taleo\.net", "account"),
    ("SuccessFactors", r"successfactors\.|sapsf\.", "account"),
    ("Oracle HCM", r"oraclecloud\.com", "account"),
    ("iCIMS", r"icims\.com", "account"),
]
_FILE_INPUT = re.compile(r"""type=["']file["']""", re.I)
_EMAIL_CV = re.compile(r"(send|email|mail)[^.<]{0,60}(resume|cv)[^.<]{0,80}[\w.+-]+@[\w-]+\.[\w.]+", re.I)
_JOB_BOARD = re.compile(r"linkedin\.com/jobs|indeed\.com", re.I)

_NOTES = {
    "easy": "a standard application form, usually without creating an account",
    "medium": "the company's own form; it may ask for extra details or an account",
    "account": "you will have to create an account (and verify it) before applying",
}


# A careers page that shows next to no text to an automated reader. Seen live on 2026-10-09 (LG Soft India on
# Darwinbox): the careers home came back as the company name and nothing else, and its job API refuses anything
# that is not a normal browser, so the job list, and so each job's own address, cannot be read automatically.
_MIN_VISIBLE_CHARS = 80
_STRIP = re.compile(r"(?is)<(script|style|noscript|head)\b.*?</\1>|<!--.*?-->|<[^>]+>")


def visible_text_length(html: str) -> int:
    return len(re.sub(r"\s+", " ", _STRIP.sub(" ", html or "")).strip())


@dataclass
class ApplyMethod:
    name: str
    level: str  # easy | medium | account | email | board | own_form | unknown
    note: str
    thin_page: bool = False  # the pages read showed almost no text: the jobs on them could not be seen

    def line(self) -> str:
        return f"{self.name}: {self.note}" if self.name else self.note


def _best(found: list[tuple[str, str]]) -> ApplyMethod:
    """Pick the most telling signal: a named system, else an upload form, else email, else a job board link."""
    for name, level in found:
        if level in ("easy", "medium", "account") and name not in ("Own upload form",):
            return ApplyMethod(name, level, _NOTES[level])
    names = [n for n, _l in found]
    if "Own upload form" in names:
        return ApplyMethod("Own upload form", "own_form", "the company's own form with a CV upload; fill it in yourself")
    if "Email your CV" in names:
        return ApplyMethod("Email your CV", "email", "apply by emailing your CV (the page gives the address)")
    if "LinkedIn/Indeed listing" in names:
        return ApplyMethod("LinkedIn/Indeed listing", "board", "applications seem to go through a LinkedIn or Indeed listing")
    return ApplyMethod("", "unknown", "no standard application system recognised on the page; open it to see")


def detect_apply_method(urls: list[str], fetch_raw: Callable[[str], tuple[str, str]], *, max_pages: int = 3) -> ApplyMethod | None:
    """Look at up to `max_pages` of the given addresses. None when none of them could be read."""
    found: list[tuple[str, str]] = []
    read = 0
    thin = 0
    for url in list(dict.fromkeys(u for u in urls if u))[:max_pages]:
        try:
            final, html = fetch_raw(url)
        except Exception:  # noqa: BLE001 - an unreadable page just contributes nothing
            continue
        read += 1
        thin += visible_text_length(html) < _MIN_VISIBLE_CHARS
        blob = f"{final} {html}"
        for name, rx, level in _SYSTEMS:
            if re.search(rx, blob, re.I):
                found.append((name, level))
        if _FILE_INPUT.search(html):
            found.append(("Own upload form", "own_form"))
        if _EMAIL_CV.search(html):
            found.append(("Email your CV", "email"))
        if _JOB_BOARD.search(blob):
            found.append(("LinkedIn/Indeed listing", "board"))
    if not read:
        return None
    method = _best(found)
    method.thin_page = thin == read
    return method
