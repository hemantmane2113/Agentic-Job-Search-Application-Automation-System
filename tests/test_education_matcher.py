from naukri_agent.config import Settings
from naukri_agent.database.models import JobExtraction
from naukri_agent.matching.education_matcher import score_education
from naukri_agent.resume.models import Education, MasterResume


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _resume(degree: str, field: str) -> MasterResume:
    return MasterResume(
        professional_summary="s",
        education=[Education(institution="Uni", degree=degree, field_of_study=field)],
    )


def test_no_requirements_gives_full_credit():
    settings = _settings()
    result = score_education(JobExtraction(education_requirements=[]), _resume("M.Sc.", "Physics"), settings)
    assert result.points == result.max_points


def test_none_extraction_gives_full_credit():
    settings = _settings()
    result = score_education(None, _resume("M.Sc.", "Physics"), settings)
    assert result.points == result.max_points


def test_matching_degree_gives_full_credit():
    settings = _settings()
    extraction = JobExtraction(education_requirements=["M.Sc."])
    result = score_education(extraction, _resume("M.Sc.", "Physics"), settings)
    assert result.points == result.max_points


def test_unmet_requirement_gives_partial_credit_not_zero():
    settings = _settings()
    extraction = JobExtraction(education_requirements=["PhD"])
    result = score_education(extraction, _resume("M.Sc.", "Physics"), settings)
    assert 0 < result.points < result.max_points
    assert result.negative_factors
