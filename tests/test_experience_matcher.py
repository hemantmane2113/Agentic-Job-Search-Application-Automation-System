import datetime

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.config import Settings
from naukri_agent.database.models import JobExtraction
from naukri_agent.matching.experience_matcher import (
    EXPERIENCE_SLIGHT_SHORTFALL_GRACE_YEARS,
    build_experience_profile,
    derive_skill_years_from_resume,
    score_experience,
)
from naukri_agent.resume.models import MasterResume, WorkExperience


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _profile(years: float) -> CandidateProfile:
    return CandidateProfile(
        full_name="X", email="x@example.com", phone="1", years_experience=years
    )


# --- score_experience: ranges ---


def test_within_range_gives_full_credit():
    settings = _settings()
    extraction = JobExtraction(experience_min=2, experience_max=5)
    result = score_experience(extraction, _dummy_exp(3), settings)
    assert result.points == result.max_points


def _dummy_exp(total_years):
    from naukri_agent.matching.models import ExperienceProfile

    return ExperienceProfile(total_years=total_years)


def test_below_minimum_gets_reduced_credit():
    settings = _settings()
    extraction = JobExtraction(experience_min=5, experience_max=8)
    result = score_experience(extraction, _dummy_exp(2), settings)
    assert 0 < result.points < result.max_points
    assert any("below" in f.lower() for f in result.negative_factors)


def test_slightly_below_minimum_gets_more_credit_than_far_below():
    settings = _settings()
    extraction = JobExtraction(experience_min=5, experience_max=8)
    close = score_experience(extraction, _dummy_exp(4.8), settings)
    far = score_experience(extraction, _dummy_exp(1), settings)
    assert close.points > far.points


def test_above_maximum_still_gets_full_credit():
    settings = _settings()
    extraction = JobExtraction(experience_min=2, experience_max=4)
    result = score_experience(extraction, _dummy_exp(10), settings)
    assert result.points == result.max_points


# --- Run 16 fix A: named shared shortfall-grace constant -------------------
#
# scorer.py's hard minimum-experience REJECT override reuses this exact
# constant, so it can never drift out of sync with this function's own
# "slightly below" partial-credit boundary.


def test_shortfall_grace_constant_is_half_a_year():
    assert EXPERIENCE_SLIGHT_SHORTFALL_GRACE_YEARS == 0.5


def test_shortfall_at_the_grace_boundary_gets_075_credit():
    settings = _settings()
    extraction = JobExtraction(experience_min=5, experience_max=8)
    result = score_experience(
        extraction, _dummy_exp(5 - EXPERIENCE_SLIGHT_SHORTFALL_GRACE_YEARS), settings
    )
    assert result.points == result.max_points * 0.75


def test_shortfall_just_beyond_the_grace_boundary_gets_025_credit():
    settings = _settings()
    extraction = JobExtraction(experience_min=5, experience_max=8)
    result = score_experience(
        extraction, _dummy_exp(5 - EXPERIENCE_SLIGHT_SHORTFALL_GRACE_YEARS - 0.1), settings
    )
    assert result.points == result.max_points * 0.25


def test_unknown_experience_requirement_gives_partial_credit_not_zero():
    settings = _settings()
    extraction = JobExtraction(experience_min=None, experience_max=None)
    result = score_experience(extraction, _dummy_exp(3), settings)
    assert 0 < result.points < result.max_points
    assert any("not specified" in f.lower() for f in result.negative_factors)


def test_none_extraction_treated_as_unknown():
    settings = _settings()
    result = score_experience(None, _dummy_exp(3), settings)
    assert 0 < result.points < result.max_points


# --- total vs relevant/skill-specific experience ---


def test_total_years_comes_from_candidate_profile_not_resume():
    profile = _profile(5)
    resume = MasterResume(professional_summary="s", work_experience=[])
    experience = build_experience_profile(profile, resume)
    assert experience.total_years == 5


def test_skill_years_derived_from_work_experience_technologies():
    today = datetime.date.today()
    resume = MasterResume(
        professional_summary="s",
        work_experience=[
            WorkExperience(
                company="A",
                title="T",
                start_date=today - datetime.timedelta(days=730),  # 2 years
                end_date=today,
                technologies=["Python", "SQL"],
            )
        ],
    )
    skill_years = derive_skill_years_from_resume(resume)
    assert skill_years["python"] == pytest_approx(2.0)
    assert skill_years["sql"] == pytest_approx(2.0)


def pytest_approx(value, abs_tol=0.05):
    import pytest

    return pytest.approx(value, abs=abs_tol)


def test_skill_years_sums_across_multiple_roles():
    today = datetime.date.today()
    resume = MasterResume(
        professional_summary="s",
        work_experience=[
            WorkExperience(
                company="A",
                title="T",
                start_date=today - datetime.timedelta(days=730),
                end_date=today - datetime.timedelta(days=365),
                technologies=["Python"],
            ),
            WorkExperience(
                company="B",
                title="T",
                start_date=today - datetime.timedelta(days=365),
                end_date=today,
                technologies=["Python"],
            ),
        ],
    )
    skill_years = derive_skill_years_from_resume(resume)
    assert skill_years["python"] == pytest_approx(2.0)


def test_skill_not_in_any_technologies_list_has_no_entry():
    """
    A skill never listed in any role's technologies has NO entry in
    skill_years at all — this is the "do not invent it" guarantee.
    We must never silently default an unknown skill to 0.0 (which
    would look like "known to have zero experience" rather than
    "we have no data").
    """
    today = datetime.date.today()
    resume = MasterResume(
        professional_summary="s",
        work_experience=[
            WorkExperience(
                company="A",
                title="T",
                start_date=today - datetime.timedelta(days=365),
                end_date=today,
                technologies=["Python"],
            )
        ],
    )
    skill_years = derive_skill_years_from_resume(resume)
    assert "aws" not in skill_years


def test_skill_years_uses_normalized_skill_names():
    today = datetime.date.today()
    resume = MasterResume(
        professional_summary="s",
        work_experience=[
            WorkExperience(
                company="A",
                title="T",
                start_date=today - datetime.timedelta(days=365),
                end_date=today,
                technologies=["ML"],
            )
        ],
    )
    skill_years = derive_skill_years_from_resume(resume)
    assert "machine learning" in skill_years
    assert "ml" not in skill_years
