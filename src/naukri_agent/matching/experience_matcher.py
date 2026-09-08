"""
Experience matching.

Two responsibilities kept deliberately separate:

1. build_experience_profile / derive_skill_years_from_resume: turn
   CandidateProfile + MasterResume into an ExperienceProfile. This is
   pure data assembly — it does not read or touch job data at all.

2. score_experience: compare an ExperienceProfile's total_years
   against a job's stated experience_min/max. This is the only part
   that's actually a "score".

skill_years is intentionally NOT used inside score_experience's
numeric score — see ExperienceProfile's docstring for why. It's
attached to the CategoryScore's factors as supporting context only,
by skill_matcher (which is where per-skill facts belong).
"""

from __future__ import annotations

import datetime

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.config import Settings
from naukri_agent.database.models import JobExtraction
from naukri_agent.matching.models import CategoryScore, ExperienceProfile
from naukri_agent.matching.skill_normalizer import normalize_skill
from naukri_agent.resume.models import MasterResume


def derive_skill_years_from_resume(resume: MasterResume) -> dict[str, float]:
    """
    Best-effort, auditable lower bound on years of experience per
    skill: for each technology listed on a work_experience entry, sum
    that entry's duration. Two roles both listing "Python" contribute
    their full individual durations (not de-duplicated for overlap),
    matching how you'd actually describe your own experience.

    This UNDER-counts by construction whenever a technology was used
    in a role but not listed in that role's `technologies` — it is a
    lower bound, not a ground truth, which is exactly why Phase 4
    does not use it to gate any score (see module docstring).
    """
    today = datetime.date.today()
    skill_days: dict[str, float] = {}

    for job in resume.work_experience:
        duration_days = max(((job.end_date or today) - job.start_date).days, 0)
        for tech in job.technologies:
            key = normalize_skill(tech)
            skill_days[key] = skill_days.get(key, 0) + duration_days

    return {skill: round(days / 365.25, 1) for skill, days in skill_days.items()}


def build_experience_profile(
    profile: CandidateProfile, resume: MasterResume
) -> ExperienceProfile:
    """
    Assemble the matching engine's view of experience. total_years
    comes from CandidateProfile — your own stated matching input,
    which may legitimately differ from a pure date-math total (e.g.
    counting research work you consider relevant). skill_years comes
    from MasterResume, since only dated work history can support it.
    """
    return ExperienceProfile(
        total_years=profile.years_experience,
        skill_years=derive_skill_years_from_resume(resume),
    )


def score_experience(
    extraction: JobExtraction | None,
    experience: ExperienceProfile,
    settings: Settings,
) -> CategoryScore:
    """
    Score the Experience category by comparing total_years against
    the job's stated experience_min/max. Missing job experience data
    is never a silent zero — see Settings.experience_unknown_credit_ratio.
    """
    max_points = settings.weight_experience
    exp_min = extraction.experience_min if extraction else None
    exp_max = extraction.experience_max if extraction else None

    if exp_min is None and exp_max is None:
        points = max_points * settings.experience_unknown_credit_ratio
        return CategoryScore(
            points=points,
            max_points=max_points,
            negative_factors=["Experience requirement not specified"],
        )

    if exp_min is not None and experience.total_years < exp_min:
        shortfall = exp_min - experience.total_years
        if shortfall <= 0.5:
            # Close enough to round to the requirement — partial credit.
            return CategoryScore(
                points=max_points * 0.75,
                max_points=max_points,
                negative_factors=[
                    f"Experience ({experience.total_years} yrs) slightly below "
                    f"minimum requirement ({exp_min} yrs)"
                ],
            )
        return CategoryScore(
            points=max_points * 0.25,
            max_points=max_points,
            negative_factors=[
                f"Experience ({experience.total_years} yrs) below minimum "
                f"requirement ({exp_min} yrs)"
            ],
        )

    if exp_max is not None and experience.total_years > exp_max:
        return CategoryScore(
            points=max_points,
            max_points=max_points,
            positive_factors=[
                f"Experience ({experience.total_years} yrs) exceeds the "
                f"stated range (up to {exp_max} yrs)"
            ],
        )

    return CategoryScore(
        points=max_points,
        max_points=max_points,
        positive_factors=["Experience within required range"],
    )
