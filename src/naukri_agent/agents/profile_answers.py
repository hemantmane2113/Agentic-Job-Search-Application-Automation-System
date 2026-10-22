"""
Deterministic answers to application screening questions, taken ONLY from
the candidate's own profile. No LLM is involved anywhere in this module.

The rule is: answer only when the profile gives ONE clear value for exactly
what was asked; otherwise return answer=None and the caller must stop and
hand the application to the human. Never guess, never infer absence (a
skill missing from the profile is "not known", not "no experience"), and
never answer anything about money -- expected pay is a range in the
profile, and current pay is not in the profile at all (a small LLM once
invented a figure for exactly that question).

Recognised question shapes (everything else is None):
  - total experience           -> CandidateProfile.years_experience
  - experience in <skill>      -> years from the resume, if the skill is
                                  in the profile and its years are known
  - "do you have <skill>?"     -> "Yes" if the skill is in the profile
  - notice period              -> CandidateProfile.notice_period_days
  - willing to relocate to <X> -> "Yes" only if X is a preferred location
"""

from __future__ import annotations

import re
from typing import NamedTuple

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.matching.models import ExperienceProfile
from naukri_agent.matching.semantic_skill_matcher import strip_qualifier_suffix
from naukri_agent.matching.skill_normalizer import find_match, normalize_skill


class ProfileAnswer(NamedTuple):
    answer: str | None  # None == do not answer; stop and ask the human
    basis: str  # why: what the answer came from, or why it was refused


_MONEY_RE = re.compile(r"\b(ctc|salary|salaries|compensation|lpa|lakhs?|lacs?|package|remuneration|pay|stipend)\b", re.I)
_HOW_MANY_RE = re.compile(r"\b(how many|years?|yrs?|number of years)\b", re.I)
_YES_NO_START_RE = re.compile(r"^\s*(do|does|did|have|has|are|is|can|could|would|will)\b", re.I)
_SKILL_PHRASE_RE = re.compile(
    r"(?:experience|exp\.?|expertise|knowledge|worked|working|proficien\w*)\s*(?:\w+\s+){0,3}?(?:in|with|on|of)\s+"
    r"([A-Za-z0-9+#./\- ]{2,40}?)\s*(?:[?.,;]|$|\bdo\b|\bhave\b|\bare\b|\bfor\b|\bin years\b)",
    re.I,
)
_TOTAL_EXP_RE = re.compile(r"\b(total|overall|work)\b[^?]{0,25}\bexperience\b|\bexperience\b[^?]{0,10}\b(in )?years\b", re.I)
_RELOCATE_RE = re.compile(r"relocat", re.I)
_RELOCATE_TO_RE = re.compile(r"relocat\w*\s+(?:to|in|at)\s+([A-Za-z .,/\-]{2,40}?)\s*(?:[?.;]|$|\bfor\b|\bif\b|\bwithin\b)", re.I)

_CITY_ALIASES = {
    "bangalore": "bengaluru", "bengaluru": "bengaluru", "blr": "bengaluru",
    "gurgaon": "gurugram", "gurugram": "gurugram",
    "hydrabad": "hyderabad", "hyderabad": "hyderabad",
    "bombay": "mumbai", "mumbai": "mumbai",
    "madras": "chennai", "chennai": "chennai",
    "pune": "pune", "noida": "noida", "delhi": "delhi", "new delhi": "delhi", "remote": "remote",
}


def _city(raw: str) -> str:
    key = re.sub(r"[^a-z ]+", " ", raw.lower()).strip()
    key = re.sub(r"\s+", " ", key)
    return _CITY_ALIASES.get(key, key)


def _fmt_years(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _refuse(basis: str) -> ProfileAnswer:
    return ProfileAnswer(None, basis)


def answer_from_profile(
    question: str, candidate: CandidateProfile, experience: ExperienceProfile
) -> ProfileAnswer:
    q = " ".join((question or "").split())
    if not q:
        return _refuse("empty question")

    if _MONEY_RE.search(q):
        return _refuse("pay questions are never answered automatically")

    if "notice" in q.lower():
        days = candidate.notice_period_days
        if days is None:
            return _refuse("notice period is not in the profile")
        return ProfileAnswer("Immediate" if days == 0 else f"{days} days", "CandidateProfile.notice_period_days")

    if _RELOCATE_RE.search(q):
        m = _RELOCATE_TO_RE.search(q)
        if not m:
            return _refuse("relocation question without a clear destination")
        wanted = _city(m.group(1))
        preferred = {_city(loc) for loc in candidate.preferred_locations}
        if wanted in preferred:
            return ProfileAnswer("Yes", f"{m.group(1).strip()!r} is a preferred location")
        return _refuse(f"{m.group(1).strip()!r} is not a preferred location (never answered 'No' on your behalf)")

    skill_match = _SKILL_PHRASE_RE.search(q)
    if skill_match and skill_match.group(1).strip(" .,-").lower() in ("year", "years", "yr", "yrs"):
        skill_match = None  # "experience in years" is a unit, not a skill
    if skill_match:
        phrase = skill_match.group(1).strip(" .,-")
        matched = find_match(phrase, candidate.skills) or find_match(strip_qualifier_suffix(phrase), candidate.skills)
        if matched is None:
            return _refuse(f"{phrase!r} is not a skill in the profile (absence is not asserted)")
        years = experience.skill_years.get(normalize_skill(matched))
        if _HOW_MANY_RE.search(q):
            if years is None or years <= 0:
                return _refuse(f"years of {matched} are not known")
            return ProfileAnswer(_fmt_years(years), f"resume-derived years of {matched}")
        if _YES_NO_START_RE.match(q):
            return ProfileAnswer("Yes", f"{matched} is in the profile skills")
        return _refuse("could not tell whether a number or yes/no was asked")

    if _TOTAL_EXP_RE.search(q) and _HOW_MANY_RE.search(q):
        return ProfileAnswer(_fmt_years(candidate.years_experience), "CandidateProfile.years_experience")

    return _refuse("question is not one of the shapes answered automatically")
