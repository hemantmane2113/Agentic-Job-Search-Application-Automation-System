"""
Role matching: is this job's title one the candidate is looking for.

Deliberately simple and literal for Phase 4 — exact match after
normalization, or substring containment (e.g. "Senior Data Scientist"
contains "Data Scientist"). No semantic/LLM-based role similarity;
that would blur the line this whole phase exists to keep sharp.
"""

from __future__ import annotations

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.config import Settings
from naukri_agent.database.models import Job, JobExtraction
from naukri_agent.matching.models import CategoryScore


def score_role(
    job: Job,
    extraction: JobExtraction | None,
    profile: CandidateProfile,
    settings: Settings,
) -> CategoryScore:
    max_points = settings.weight_role

    if not profile.preferred_roles:
        return CategoryScore(
            points=max_points,
            max_points=max_points,
            positive_factors=["No preferred role restriction set"],
        )

    title = (extraction.normalized_title if extraction and extraction.normalized_title else job.title)
    title_lower = title.strip().lower()

    for preferred in profile.preferred_roles:
        preferred_lower = preferred.strip().lower()
        if preferred_lower == title_lower:
            return CategoryScore(
                points=max_points,
                max_points=max_points,
                positive_factors=[f"Role title exactly matches '{preferred}'"],
            )

    for preferred in profile.preferred_roles:
        preferred_lower = preferred.strip().lower()
        if preferred_lower in title_lower or title_lower in preferred_lower:
            return CategoryScore(
                points=max_points * 0.75,
                max_points=max_points,
                positive_factors=[f"Role title closely matches '{preferred}'"],
            )

    return CategoryScore(
        points=0,
        max_points=max_points,
        negative_factors=[f"Role title '{title}' does not match preferred roles"],
    )
