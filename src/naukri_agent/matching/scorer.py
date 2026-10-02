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
from naukri_agent.jobs.parser import (
    classify_experience_requirements,
    format_experience_conflict_message,
    format_experience_exclude_multi_message,
)
from naukri_agent.matching.experience_matcher import (
    EXPERIENCE_SLIGHT_SHORTFALL_GRACE_YEARS,
    build_experience_profile,
    score_experience,
)
from naukri_agent.matching.education_matcher import score_education
from naukri_agent.matching.location_matcher import score_location
from naukri_agent.matching.models import CategoryScore, MatchDecision, MatchResult
from naukri_agent.matching.role_matcher import score_role
from naukri_agent.matching.salary_matcher import score_salary
from naukri_agent.matching.skill_matcher import (
    format_required_skill_coverage_message,
    required_skill_coverage,
    required_skill_coverage_below_floor,
    score_skills,
)
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

    # Deterministic, conflict-aware minimum-experience decision (Part A,
    # extended). Derived FRESH from job.experience_text + job.description
    # -- independent of whatever extraction.experience_min/max holds --
    # via classify_experience_requirements (jobs/parser.py), which builds
    # a bounded candidate list (Naukri structured field + every
    # requirement-shaped JD-body statement) and decides candidate-
    # relatively whether they agree, conflict-with-something-admitting,
    # or conflict-with-everything-excluding. This is the ONE hard-reject
    # mechanism for experience -- not a second, competing override.
    #   "admit"          -> every candidate admits: no override, the
    #                        normal threshold-based decision computed
    #                        below stands (score_experience's own
    #                        tiering already reflects this correctly).
    #   "exclude_single"  -> one clear minimum, candidate below it ->
    #                        REJECT (score_experience's own existing
    #                        "below minimum" wording already explains
    #                        this; category_scores left untouched).
    #   "exclude_multi"   -> multiple DISTINCT minimums, ALL exclude the
    #                        candidate -> REJECT, with a message naming
    #                        every source found.
    #   "conflict"        -> candidates disagree and at least one admits
    #                        -> REVIEW, capped even if the aggregate
    #                        score would otherwise be ACCEPT -- a
    #                        potentially-eligible job is never silently
    #                        discarded, and a doubtful one is never
    #                        silently accepted either.
    #   "unknown"         -> neither deterministic source parsed at all;
    #                        fall back to the pre-existing behaviour of
    #                        trusting extraction.experience_min/max
    #                        (the LLM's own best-effort number, however
    #                        FIX 2 above left it) exactly as before this
    #                        change, so jobs where only the LLM found a
    #                        number are unaffected.
    exp_classification = classify_experience_requirements(job, experience_profile.total_years)
    exp_max_points = category_scores["experience"].max_points
    if exp_classification.state == "conflict":
        category_scores["experience"] = CategoryScore(
            points=exp_max_points * settings.experience_unknown_credit_ratio,
            max_points=exp_max_points,
            negative_factors=[format_experience_conflict_message(exp_classification)],
        )
    elif exp_classification.state == "exclude_multi":
        category_scores["experience"] = CategoryScore(
            points=exp_max_points * 0.25,
            max_points=exp_max_points,
            negative_factors=[
                format_experience_exclude_multi_message(
                    exp_classification, experience_profile.total_years
                )
            ],
        )

    total_points = sum(cs.points for cs in category_scores.values())
    total_max = sum(cs.max_points for cs in category_scores.values())
    overall_score = round((total_points / total_max) * 100, 1) if total_max > 0 else 0.0

    if overall_score >= settings.threshold_accept:
        decision = MatchDecision.ACCEPT
    elif overall_score >= settings.threshold_review:
        decision = MatchDecision.REVIEW
    else:
        decision = MatchDecision.REJECT

    if exp_classification.state in ("exclude_single", "exclude_multi"):
        decision = MatchDecision.REJECT
    elif exp_classification.state == "conflict":
        decision = MatchDecision.REVIEW
    elif exp_classification.state == "unknown":
        if (
            extraction is not None
            and extraction.experience_min is not None
            and experience_profile.total_years
            < extraction.experience_min - EXPERIENCE_SLIGHT_SHORTFALL_GRACE_YEARS
        ):
            decision = MatchDecision.REJECT
    # "admit" -> no override; the threshold-based decision above stands.

    # Deterministic required-skill coverage gate (Run 17 skill-quality
    # follow-up), independent of the experience override above and of
    # score_skills()'s own blended points -- it only ever CAPS an
    # ACCEPT down to REVIEW, never REJECTs and never raises a REJECT.
    # Computed fresh from extraction.required_skills + profile.skills,
    # mirroring how classify_experience_requirements is read separately
    # from score_experience()'s own points. required_skills == [] ->
    # required_skill_coverage() returns None -> no gate, existing
    # behavior preserved exactly (approved: never gate on preferred-only
    # or blended required+preferred coverage).
    skill_coverage = required_skill_coverage(extraction, profile.skills)
    if skill_coverage is not None and required_skill_coverage_below_floor(skill_coverage):
        category_scores["skills"] = category_scores["skills"].model_copy(
            update={
                "negative_factors": [
                    *category_scores["skills"].negative_factors,
                    format_required_skill_coverage_message(skill_coverage),
                ]
            }
        )
        if decision == MatchDecision.ACCEPT:
            decision = MatchDecision.REVIEW

    positive_factors = [f for cs in category_scores.values() for f in cs.positive_factors]
    negative_factors = [f for cs in category_scores.values() for f in cs.negative_factors]

    return MatchResult(
        overall_score=overall_score,
        decision=decision,
        category_scores=category_scores,
        positive_factors=positive_factors,
        negative_factors=negative_factors,
    )
