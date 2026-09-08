from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.config import Settings
from naukri_agent.database.models import Job, JobExtraction
from naukri_agent.matching.role_matcher import score_role


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _job(title: str) -> Job:
    return Job(
        url="https://example.com/1",
        title=title,
        company="Acme",
        location="Pune",
        description="d",
        content_fingerprint="fp",
    )


def _profile(roles: list[str]) -> CandidateProfile:
    return CandidateProfile(
        full_name="X", email="x@example.com", phone="1", preferred_roles=roles
    )


def test_no_preferred_roles_gives_full_credit():
    settings = _settings()
    result = score_role(_job("Anything"), None, _profile([]), settings)
    assert result.points == result.max_points


def test_exact_match_gives_full_credit():
    settings = _settings()
    result = score_role(_job("Data Scientist"), None, _profile(["Data Scientist"]), settings)
    assert result.points == result.max_points


def test_substring_match_gives_partial_credit():
    settings = _settings()
    result = score_role(
        _job("Senior Data Scientist"), None, _profile(["Data Scientist"]), settings
    )
    assert 0 < result.points < result.max_points


def test_no_match_gives_zero_and_negative_factor():
    settings = _settings()
    result = score_role(_job("Sales Executive"), None, _profile(["Data Scientist"]), settings)
    assert result.points == 0
    assert result.negative_factors


def test_uses_normalized_title_from_extraction_when_available():
    settings = _settings()
    extraction = JobExtraction(normalized_title="Data Scientist")
    result = score_role(_job("DS - Team X"), extraction, _profile(["Data Scientist"]), settings)
    assert result.points == result.max_points
