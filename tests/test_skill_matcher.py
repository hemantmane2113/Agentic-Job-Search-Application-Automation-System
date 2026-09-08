from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.config import Settings
from naukri_agent.database.models import JobExtraction
from naukri_agent.matching.models import ExperienceProfile
from naukri_agent.matching.skill_matcher import score_skills


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _profile(skills: list[str]) -> CandidateProfile:
    return CandidateProfile(full_name="X", email="x@example.com", phone="1", skills=skills)


def _extraction(required=None, preferred=None) -> JobExtraction:
    return JobExtraction(required_skills=required or [], preferred_skills=preferred or [])


def test_all_required_skills_matched_gives_full_credit():
    settings = _settings()
    profile = _profile(["Python", "SQL"])
    extraction = _extraction(required=["Python", "SQL"])
    result = score_skills(extraction, profile, ExperienceProfile(total_years=3), settings)
    assert result.points == result.max_points


def test_missing_required_skill_costs_more_than_missing_preferred():
    settings = _settings()
    profile = _profile(["Python"])

    missing_required = _extraction(required=["Python", "SQL"], preferred=[])
    missing_preferred = _extraction(required=["Python"], preferred=["SQL"])

    score_missing_required = score_skills(
        missing_required, profile, ExperienceProfile(total_years=3), settings
    )
    score_missing_preferred = score_skills(
        missing_preferred, profile, ExperienceProfile(total_years=3), settings
    )
    assert score_missing_required.points < score_missing_preferred.points


def test_missing_required_skill_produces_negative_factor():
    settings = _settings()
    profile = _profile(["Python"])
    extraction = _extraction(required=["Python", "AWS"])
    result = score_skills(extraction, profile, ExperienceProfile(total_years=3), settings)
    assert any("AWS" in f for f in result.negative_factors)


def test_matched_required_skill_produces_positive_factor():
    settings = _settings()
    profile = _profile(["Python"])
    extraction = _extraction(required=["Python"])
    result = score_skills(extraction, profile, ExperienceProfile(total_years=3), settings)
    assert any("Python" in f for f in result.positive_factors)


def test_skill_alias_is_recognized_as_match():
    settings = _settings()
    profile = _profile(["Machine Learning"])
    extraction = _extraction(required=["ML"])
    result = score_skills(extraction, profile, ExperienceProfile(total_years=3), settings)
    assert result.points == result.max_points


def test_no_job_skills_specified_gives_full_credit():
    settings = _settings()
    profile = _profile([])
    extraction = _extraction(required=[], preferred=[])
    result = score_skills(extraction, profile, ExperienceProfile(total_years=3), settings)
    assert result.points == result.max_points


def test_none_extraction_treated_as_no_requirements():
    settings = _settings()
    profile = _profile(["Python"])
    result = score_skills(None, profile, ExperienceProfile(total_years=3), settings)
    assert result.points == result.max_points


def test_skill_years_annotated_in_positive_factor_when_available():
    settings = _settings()
    profile = _profile(["Python"])
    extraction = _extraction(required=["Python"])
    experience = ExperienceProfile(total_years=3, skill_years={"python": 4.2})
    result = score_skills(extraction, profile, experience, settings)
    assert any("4.2" in f for f in result.positive_factors)


def test_no_skill_years_data_gives_plain_factor_not_invented():
    settings = _settings()
    profile = _profile(["Python"])
    extraction = _extraction(required=["Python"])
    experience = ExperienceProfile(total_years=3, skill_years={})
    result = score_skills(extraction, profile, experience, settings)
    assert result.positive_factors == ["Python required skill matched"]
