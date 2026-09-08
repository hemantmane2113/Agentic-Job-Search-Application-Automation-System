"""
Skill matching.

Required and preferred skills are scored as two separate coverage
ratios, then combined with a configurable split
(Settings.required_skills_weight_ratio) so a missing required skill
costs meaningfully more than a missing preferred one — not as a
special case, but as a direct consequence of the weighting.
"""

from __future__ import annotations

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.config import Settings
from naukri_agent.database.models import JobExtraction
from naukri_agent.matching.models import CategoryScore, ExperienceProfile
from naukri_agent.matching.skill_normalizer import find_match, normalize_skill


def _coverage(
    job_skills: list[str],
    candidate_skills: list[str],
    experience: ExperienceProfile,
    label: str,
) -> tuple[float, list[str], list[str]]:
    """
    Return (matched_ratio, positive_factors, negative_factors) for one
    list of job-required skills (either "required" or "preferred").
    An empty job_skills list means "nothing to satisfy" -> full ratio.
    """
    if not job_skills:
        return 1.0, [], []

    positives: list[str] = []
    negatives: list[str] = []
    matched = 0

    for skill in job_skills:
        candidate_match = find_match(skill, candidate_skills)
        if candidate_match is not None:
            matched += 1
            years = experience.skill_years.get(normalize_skill(candidate_match))
            if years:
                positives.append(
                    f"{skill} {label} skill matched ({years} yrs relevant experience)"
                )
            else:
                positives.append(f"{skill} {label} skill matched")
        else:
            negatives.append(f"{skill} {label} but not present")

    return matched / len(job_skills), positives, negatives


def score_skills(
    extraction: JobExtraction | None,
    profile: CandidateProfile,
    experience: ExperienceProfile,
    settings: Settings,
) -> CategoryScore:
    max_points = settings.weight_skills
    required = extraction.required_skills if extraction and extraction.required_skills else []
    preferred = extraction.preferred_skills if extraction and extraction.preferred_skills else []

    req_ratio, req_pos, req_neg = _coverage(
        required, profile.skills, experience, "required"
    )
    pref_ratio, pref_pos, pref_neg = _coverage(
        preferred, profile.skills, experience, "preferred"
    )

    ratio_weight = settings.required_skills_weight_ratio
    combined_ratio = ratio_weight * req_ratio + (1 - ratio_weight) * pref_ratio

    return CategoryScore(
        points=max_points * combined_ratio,
        max_points=max_points,
        positive_factors=req_pos + pref_pos,
        negative_factors=req_neg + pref_neg,
    )
