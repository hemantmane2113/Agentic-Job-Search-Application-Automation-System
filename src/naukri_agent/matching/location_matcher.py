"""
Location matching. Deliberately simple: substring containment against
the candidate's preferred_locations, plus a remote-work special case.
"""

from __future__ import annotations

from naukri_agent.candidate.models import CandidateProfile, WorkMode
from naukri_agent.config import Settings
from naukri_agent.database.models import Job
from naukri_agent.matching.models import CategoryScore


def score_location(job: Job, profile: CandidateProfile, settings: Settings) -> CategoryScore:
    max_points = settings.weight_location
    location_lower = job.location.strip().lower()

    if not profile.preferred_locations:
        return CategoryScore(
            points=max_points,
            max_points=max_points,
            positive_factors=["No preferred location restriction set"],
        )

    if "remote" in location_lower and profile.work_mode in (WorkMode.REMOTE, WorkMode.ANY):
        return CategoryScore(
            points=max_points,
            max_points=max_points,
            positive_factors=["Job is remote, matching work-mode preference"],
        )

    for preferred in profile.preferred_locations:
        if preferred.strip().lower() in location_lower:
            return CategoryScore(
                points=max_points,
                max_points=max_points,
                positive_factors=[f"Location matches preference ({preferred})"],
            )

    return CategoryScore(
        points=0,
        max_points=max_points,
        negative_factors=[f"Location '{job.location}' does not match preferences"],
    )
