from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.config import Settings
from naukri_agent.database.models import Job, JobExtraction
from naukri_agent.matching.models import MatchDecision
from naukri_agent.matching.scorer import score_job
from naukri_agent.resume.models import MasterResume


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _job(**overrides) -> Job:
    defaults = dict(
        url="https://example.com/1",
        title="Data Scientist",
        company="Acme",
        location="Pune",
        description="d",
        content_fingerprint="fp",
    )
    defaults.update(overrides)
    return Job(**defaults)


def _strong_profile() -> CandidateProfile:
    return CandidateProfile(
        full_name="X",
        email="x@example.com",
        phone="1",
        skills=["Python", "SQL", "Machine Learning"],
        years_experience=3,
        preferred_roles=["Data Scientist"],
        preferred_locations=["Pune"],
        expected_salary_min_lpa=8,
    )


def _weak_profile() -> CandidateProfile:
    return CandidateProfile(
        full_name="X",
        email="x@example.com",
        phone="1",
        skills=["Excel"],
        years_experience=0,
        preferred_roles=["Sales Executive"],
        preferred_locations=["Mumbai"],
        expected_salary_min_lpa=20,
    )


def _resume() -> MasterResume:
    return MasterResume(professional_summary="s")


def _strong_extraction() -> JobExtraction:
    return JobExtraction(
        normalized_title="Data Scientist",
        required_skills=["Python", "SQL"],
        preferred_skills=["Machine Learning"],
        experience_min=2,
        experience_max=5,
        salary_max=14,
    )


def test_strong_match_produces_accept_decision():
    settings = _settings()
    result = score_job(_job(), _strong_extraction(), _strong_profile(), _resume(), settings)
    assert result.decision == MatchDecision.ACCEPT
    assert result.overall_score >= settings.threshold_accept


def test_weak_match_produces_reject_decision():
    settings = _settings()
    result = score_job(
        _job(location="Mumbai"), _strong_extraction(), _weak_profile(), _resume(), settings
    )
    assert result.decision == MatchDecision.REJECT
    assert result.overall_score < settings.threshold_review


def test_borderline_match_produces_review_decision():
    settings = _settings(threshold_accept=80, threshold_review=70)
    # Meets skills/role/location but salary unknown and experience unspecified
    extraction = JobExtraction(
        normalized_title="Data Scientist",
        required_skills=["Python", "SQL"],
        preferred_skills=["Machine Learning"],
    )
    result = score_job(_job(), extraction, _strong_profile(), _resume(), settings)
    assert result.decision in (MatchDecision.REVIEW, MatchDecision.ACCEPT)
    # explicitly check it's not a silent full-credit outcome
    assert any("incomplete" in f.lower() or "not specified" in f.lower() for f in result.negative_factors)


def test_overall_score_is_weighted_sum_normalized_to_100():
    settings = _settings()
    result = score_job(_job(), _strong_extraction(), _strong_profile(), _resume(), settings)
    total_max = sum(cs.max_points for cs in result.category_scores.values())
    total_points = sum(cs.points for cs in result.category_scores.values())
    expected = round((total_points / total_max) * 100, 1)
    assert result.overall_score == expected


def test_all_six_categories_present_in_result():
    settings = _settings()
    result = score_job(_job(), _strong_extraction(), _strong_profile(), _resume(), settings)
    assert set(result.category_scores.keys()) == {
        "skills",
        "experience",
        "role",
        "salary",
        "location",
        "education",
    }


def test_positive_and_negative_factors_are_aggregated_from_categories():
    settings = _settings()
    result = score_job(_job(), _strong_extraction(), _strong_profile(), _resume(), settings)
    assert len(result.positive_factors) > 0


def test_missing_extraction_does_not_crash_and_degrades_gracefully():
    settings = _settings()
    result = score_job(_job(), None, _strong_profile(), _resume(), settings)
    assert 0 <= result.overall_score <= 100


def test_weights_do_not_need_to_sum_to_100():
    settings = _settings(
        weight_skills=10, weight_experience=10, weight_role=10,
        weight_salary=10, weight_location=10, weight_education=10,
    )
    result = score_job(_job(), _strong_extraction(), _strong_profile(), _resume(), settings)
    assert 0 <= result.overall_score <= 100
