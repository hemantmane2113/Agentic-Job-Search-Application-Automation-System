"""
Salary matching.

Missing salary information is never a silent rejection (Section 5) —
it earns a configurable partial credit (Settings.salary_unknown_
credit_ratio) plus an explanatory negative factor, matching the
worked example in the master spec (Salary: 10/15 out of a 15-point
category when salary info was incomplete).
"""

from __future__ import annotations

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.config import Settings
from naukri_agent.database.models import JobExtraction
from naukri_agent.matching.models import CategoryScore


def score_salary(
    extraction: JobExtraction | None,
    profile: CandidateProfile,
    settings: Settings,
) -> CategoryScore:
    max_points = settings.weight_salary

    if profile.expected_salary_min_lpa is None:
        return CategoryScore(
            points=max_points,
            max_points=max_points,
            positive_factors=["No salary preference set"],
        )

    job_max = extraction.salary_max if extraction else None
    if job_max is None:
        points = max_points * settings.salary_unknown_credit_ratio
        return CategoryScore(
            points=points,
            max_points=max_points,
            negative_factors=["Salary information incomplete"],
        )

    if job_max >= profile.expected_salary_min_lpa:
        return CategoryScore(
            points=max_points,
            max_points=max_points,
            positive_factors=["Salary meets expectation"],
        )

    if job_max >= profile.expected_salary_min_lpa * settings.salary_tolerance_ratio:
        return CategoryScore(
            points=max_points * 0.6,
            max_points=max_points,
            negative_factors=[
                f"Salary (up to {job_max} LPA) close to but below expected "
                f"minimum ({profile.expected_salary_min_lpa} LPA)"
            ],
        )

    return CategoryScore(
        points=max_points * 0.2,
        max_points=max_points,
        negative_factors=[
            f"Salary (up to {job_max} LPA) below expected minimum "
            f"({profile.expected_salary_min_lpa} LPA)"
        ],
    )
