"""
Education/"other" matching — the smallest-weight category (default
5%). Kept intentionally simple: if the job states no education
requirements, full credit (nothing to fail against). If it does, a
literal substring check against the candidate's stated degrees —
no inference about equivalent qualifications, since that's exactly
the kind of judgment call this system should not make silently.
"""

from __future__ import annotations

from naukri_agent.config import Settings
from naukri_agent.database.models import JobExtraction
from naukri_agent.matching.models import CategoryScore
from naukri_agent.resume.models import MasterResume


def score_education(
    extraction: JobExtraction | None,
    resume: MasterResume,
    settings: Settings,
) -> CategoryScore:
    max_points = settings.weight_education
    requirements = extraction.education_requirements if extraction else None

    if not requirements:
        return CategoryScore(points=max_points, max_points=max_points)

    candidate_degrees = [
        f"{edu.degree} {edu.field_of_study or ''}".strip().lower()
        for edu in resume.education
    ]

    for requirement in requirements:
        req_lower = requirement.strip().lower()
        if any(req_lower in degree or degree in req_lower for degree in candidate_degrees):
            return CategoryScore(
                points=max_points,
                max_points=max_points,
                positive_factors=[f"Education requirement '{requirement}' satisfied"],
            )

    return CategoryScore(
        points=max_points * 0.5,
        max_points=max_points,
        negative_factors=[f"Education requirements ({', '.join(requirements)}) not clearly met"],
    )
