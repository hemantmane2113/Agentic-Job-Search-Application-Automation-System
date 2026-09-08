"""
JobScorer: the single place that combines every category's
CategoryScore into an overall MatchResult and an ACCEPT/REVIEW/REJECT
decision.

This is the deterministic gate described in the master spec:

    JobExtraction -> JobScorer -> weighted score -> ACCEPT/REVIEW/REJECT

No LLM call happens anywhere in this module or the sub-scorers it
calls. An LLM may have produced the JobExtraction this function reads
(Phase 5), but the decision itself is 100% Python.
"""

from __future__ import annotations

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.config import Settings
from naukri_agent.database.models import Job, JobExtraction
from naukri_agent.matching.experience_matcher import build_experience_profile, score_experience
from naukri_agent.matching.education_matcher import score_education
from naukri_agent.matching.location_matcher import score_location
from naukri_agent.matching.models import MatchDecision, MatchResult
from naukri_agent.matching.role_matcher import score_role
from naukri_agent.matching.salary_matcher import score_salary
from naukri_agent.matching.skill_matcher import score_skills
from naukri_agent.resume.models import MasterResume


def score_job(
    job: Job,
    extraction: JobExtraction | None,
    profile: CandidateProfile,
    resume: MasterResume,
    settings: Settings,
) -> MatchResult:
    """
    Score one job against one candidate. extraction may be None (a
    job that hasn't been through Phase 5's LLM parser yet) — every
    sub-scorer treats a missing extraction the same as an extraction
    with all-empty derived fields, so scoring degrades gracefully
    rather than failing.
    """
    experience_profile = build_experience_profile(profile, resume)

    category_scores = {
        "skills": score_skills(extraction, profile, experience_profile, settings),
        "experience": score_experience(extraction, experience_profile, settings),
        "role": score_role(job, extraction, profile, settings),
        "salary": score_salary(extraction, profile, settings),
        "location": score_location(job, profile, settings),
        "education": score_education(extraction, resume, settings),
    }

    total_points = sum(cs.points for cs in category_scores.values())
    total_max = sum(cs.max_points for cs in category_scores.values())
    overall_score = round((total_points / total_max) * 100, 1) if total_max > 0 else 0.0

    if overall_score >= settings.threshold_accept:
        decision = MatchDecision.ACCEPT
    elif overall_score >= settings.threshold_review:
        decision = MatchDecision.REVIEW
    else:
        decision = MatchDecision.REJECT

    positive_factors = [f for cs in category_scores.values() for f in cs.positive_factors]
    negative_factors = [f for cs in category_scores.values() for f in cs.negative_factors]

    return MatchResult(
        overall_score=overall_score,
        decision=decision,
        category_scores=category_scores,
        positive_factors=positive_factors,
        negative_factors=negative_factors,
    )
