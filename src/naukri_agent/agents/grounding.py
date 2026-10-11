"""
Shared anti-fabrication grounding facts for agentic drafting modules
(apply_answer_agent.py).

Factored out so the whitelist of permitted CandidateProfile/MasterResume
facts can never silently drift between the two agents -- both must draw
on exactly the same subset, never the full model, never anything not
listed here.
"""

from __future__ import annotations

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.resume.models import MasterResume


def candidate_facts(candidate: CandidateProfile) -> dict:
    """Flattened, JSON-safe facts a draft is permitted to draw on.
    Deliberately a plain subset dump, not the full model -- nothing
    here should ever reference internal-only fields."""
    return {
        "full_name": candidate.full_name,
        "years_experience": candidate.years_experience,
        "skills": candidate.skills,
        "preferred_roles": candidate.preferred_roles,
        "preferred_locations": candidate.preferred_locations,
        "work_mode": candidate.work_mode.value,
        "expected_salary_min_lpa": candidate.expected_salary_min_lpa,
        "expected_salary_max_lpa": candidate.expected_salary_max_lpa,
        "notice_period_days": candidate.notice_period_days,
    }


def resume_facts(resume: MasterResume) -> dict:
    return {
        "professional_summary": resume.professional_summary,
        "skills": resume.skills,
        "total_years_experience": resume.total_years_experience(),
        "work_experience": [
            {
                "company": w.company,
                "title": w.title,
                "start_date": str(w.start_date),
                "end_date": str(w.end_date) if w.end_date else "present",
                "technologies": w.technologies,
            }
            for w in resume.work_experience
        ],
        "education": [
            {"institution": e.institution, "degree": e.degree, "field_of_study": e.field_of_study}
            for e in resume.education
        ],
        "certifications": [c.name for c in resume.certifications],
    }
