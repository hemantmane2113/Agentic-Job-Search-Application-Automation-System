"""
Skill matching.

Required and preferred skills are scored as two separate coverage
ratios, then combined with a configurable split
(Settings.required_skills_weight_ratio) so a missing required skill
costs meaningfully more than a missing preferred one — not as a
special case, but as a direct consequence of the weighting.

2026-09-12: matching is now a 3-tier hybrid (design approved same day)
— Tier 1 (this module's original exact/alias matching, UNCHANGED) is
tried first; matching/semantic_skill_matcher.py adds Tier 2
(deterministic compound/qualifier decomposition, zero LLM cost) and
Tier 3 (one batched LLM call per job, only for whatever remains
unresolved). score_skills() gained one new OPTIONAL keyword argument
(llm_provider, default None) — every existing caller that doesn't pass
it (matching/scorer.py, unchanged) gets Tiers 1-2 only, which already
fixes compound/qualifier cases like "Machine Learning/AI" and "Python
programming" at zero LLM cost; Tier 3 activates only when a caller
explicitly supplies a provider.
"""

from __future__ import annotations

from typing import NamedTuple

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.config import Settings
from naukri_agent.database.models import JobExtraction
from naukri_agent.llm.base import LLMProvider
from naukri_agent.matching.models import CategoryScore, ExperienceProfile
from naukri_agent.matching.semantic_skill_matcher import TierResolution, resolve_skills_for_job
from naukri_agent.matching.skill_normalizer import find_match, normalize_skill


def _positive_factor_text(skill: str, label: str, resolution: TierResolution, years: float | None) -> str:
    suffix = f" ({years} yrs relevant experience)" if years else ""
    if resolution.tier == "tier1":
        # Byte-identical to the pre-2026-09-12 wording -- regression compatibility.
        return f"{skill} {label} skill matched{suffix}"
    if resolution.tier == "tier2_and":
        return f"{skill} {label} skill matched (all parts present){suffix}"
    return f"{skill} {label} skill matched via {resolution.matched_candidate_skill}{suffix}"


def _coverage(
    job_skills: list[str],
    label: str,
    experience: ExperienceProfile,
    resolution: dict[tuple[str, str], TierResolution],
) -> tuple[float, list[str], list[str]]:
    """
    Return (matched_ratio, positive_factors, negative_factors) for one
    list of job-required skills (either "required" or "preferred").
    An empty job_skills list means "nothing to satisfy" -> full ratio.
    `resolution` is pre-computed once per job (both lists together) by
    resolve_skills_for_job() so a single Tier-3 LLM call can cover every
    unresolved skill across both lists, not one call per list/skill.
    """
    if not job_skills:
        return 1.0, [], []

    positives: list[str] = []
    negatives: list[str] = []
    matched = 0

    for skill in job_skills:
        r = resolution[(label, skill)]
        if r.matched:
            matched += 1
            years = (
                experience.skill_years.get(normalize_skill(r.matched_candidate_skill))
                if r.matched_candidate_skill
                else None
            )
            positives.append(_positive_factor_text(skill, label, r, years))
        else:
            negatives.append(f"{skill} {label} but not present")

    return matched / len(job_skills), positives, negatives


def score_skills(
    extraction: JobExtraction | None,
    profile: CandidateProfile,
    experience: ExperienceProfile,
    settings: Settings,
    llm_provider: LLMProvider | None = None,
) -> CategoryScore:
    max_points = settings.weight_skills
    required = extraction.required_skills if extraction and extraction.required_skills else []
    preferred = extraction.preferred_skills if extraction and extraction.preferred_skills else []

    resolution = resolve_skills_for_job(required, preferred, profile.skills, llm_provider)

    req_ratio, req_pos, req_neg = _coverage(required, "required", experience, resolution)
    pref_ratio, pref_pos, pref_neg = _coverage(preferred, "preferred", experience, resolution)

    # required_skills=[] must never grant a free 0.8x credit toward a
    # category the JD never actually populated with a "required" list --
    # _coverage([]) == 1.0 is correct in isolation ("nothing to fail
    # against"), but combining that with the 0.8/0.2 split silently
    # rewarded jobs where the model filed everything under "preferred"
    # instead (Run 17: Wabtec 7/17 preferred -> scored as if 88% skills
    # coverage). When nothing was classified required, the preferred
    # list -- whatever the model actually extracted -- carries the full
    # weight instead of being capped at (1 - required_skills_weight_ratio).
    # Both lists empty is unchanged: genuinely nothing to fail against.
    if required:
        ratio_weight = settings.required_skills_weight_ratio
        combined_ratio = ratio_weight * req_ratio + (1 - ratio_weight) * pref_ratio
    elif preferred:
        combined_ratio = pref_ratio
    else:
        combined_ratio = 1.0

    return CategoryScore(
        points=max_points * combined_ratio,
        max_points=max_points,
        positive_factors=req_pos + pref_pos,
        negative_factors=req_neg + pref_neg,
    )


# --- Required-skill coverage gate (Run 17 skill-quality follow-up) --------
#
# A second, independent signal from the blended points above: "did the
# candidate match enough of what the JD explicitly classified as
# REQUIRED" -- deliberately never blended with preferred coverage and
# deliberately not applied at all when required_skills is empty, per
# the approved investigation:
#   - preferred skills are optional by the LLM's own classification, so
#     a large preferred list must never make a strong required-skill
#     match look bad (ratio-based, and now doubly so: this gate never
#     even looks at preferred);
#   - required_skills == [] means the JD never confirmed anything was
#     mandatory -- there is no "failed requirement" signal to gate on,
#     so existing behavior is preserved exactly in that case (this
#     function returns None and callers must treat that as "no gate").
# This does NOT change score_skills()'s points/ratio in any way -- it
# is read separately by matching/scorer.py to cap the decision, exactly
# like classify_experience_requirements() is read independently of
# score_experience().


class RequiredSkillCoverage(NamedTuple):
    """matched/total counts (required skills only) and their ratio."""

    matched: int
    total: int
    ratio: float


REQUIRED_SKILL_COVERAGE_REVIEW_FLOOR = 0.60


def required_skill_coverage(
    extraction: JobExtraction | None,
    candidate_skills: list[str],
) -> RequiredSkillCoverage | None:
    """
    Return required-skill match counts, or None when required_skills is
    empty/missing -- signalling "no gate applies here" to the caller.
    Uses the exact same find_match() matching score_skills() uses; no
    new matching logic, no fuzzy/substring comparison.
    """
    required = extraction.required_skills if extraction and extraction.required_skills else []
    if not required:
        return None
    matched = sum(1 for skill in required if find_match(skill, candidate_skills) is not None)
    return RequiredSkillCoverage(matched=matched, total=len(required), ratio=matched / len(required))


def required_skill_coverage_below_floor(coverage: RequiredSkillCoverage) -> bool:
    """
    True when coverage.ratio is strictly below the 60% floor. Compared
    via integer cross-multiplication (not float ratio <
    REQUIRED_SKILL_COVERAGE_REVIEW_FLOOR) so an exact boundary case like
    3/5 == 0.6 can never be pushed to the wrong side by float rounding.
    """
    floor_pct = round(REQUIRED_SKILL_COVERAGE_REVIEW_FLOOR * 100)
    return coverage.matched * 100 < coverage.total * floor_pct


def format_required_skill_coverage_message(coverage: RequiredSkillCoverage) -> str:
    pct = round(coverage.ratio * 100)
    return (
        f"⚠️ Required skill coverage is {pct}% ({coverage.matched}/{coverage.total}). "
        "Review required skills before applying."
    )
