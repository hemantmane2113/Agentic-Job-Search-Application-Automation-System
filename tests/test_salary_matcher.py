from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.config import Settings
from naukri_agent.database.models import JobExtraction
from naukri_agent.matching.salary_matcher import score_salary


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _profile(min_salary=None) -> CandidateProfile:
    return CandidateProfile(
        full_name="X", email="x@example.com", phone="1", expected_salary_min_lpa=min_salary
    )


def test_no_candidate_preference_gives_full_credit():
    settings = _settings()
    result = score_salary(JobExtraction(salary_max=6), _profile(None), settings)
    assert result.points == result.max_points


def test_job_meets_expectation_gives_full_credit():
    settings = _settings()
    result = score_salary(JobExtraction(salary_max=14), _profile(10), settings)
    assert result.points == result.max_points
    assert any("meets" in f.lower() for f in result.positive_factors)


def test_unknown_salary_gives_configured_partial_credit_matching_spec_example():
    """
    The master spec's own worked example shows Salary: 10/15 when
    salary info is incomplete — the default salary_unknown_credit_ratio
    (0.67) should reproduce that exactly.
    """
    settings = _settings(weight_salary=15, salary_unknown_credit_ratio=0.67)
    result = score_salary(JobExtraction(salary_max=None), _profile(10), settings)
    assert round(result.points) == 10
    assert any("incomplete" in f.lower() for f in result.negative_factors)


def test_none_extraction_treated_as_unknown_salary():
    settings = _settings()
    result = score_salary(None, _profile(10), settings)
    assert 0 < result.points < result.max_points


def test_salary_close_to_expectation_gets_partial_credit():
    settings = _settings(salary_tolerance_ratio=0.9)
    # job max 9, candidate wants min 10 -> 9/10 = 0.9, right at tolerance boundary
    result = score_salary(JobExtraction(salary_max=9), _profile(10), settings)
    assert 0 < result.points < result.max_points


def test_salary_far_below_expectation_gets_low_credit():
    settings = _settings()
    result = score_salary(JobExtraction(salary_max=4), _profile(10), settings)
    assert result.points < result.max_points * 0.5
    assert any("below" in f.lower() for f in result.negative_factors)
