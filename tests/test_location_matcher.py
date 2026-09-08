from naukri_agent.candidate.models import CandidateProfile, WorkMode
from naukri_agent.config import Settings
from naukri_agent.database.models import Job
from naukri_agent.matching.location_matcher import score_location


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _job(location: str) -> Job:
    return Job(
        url="https://example.com/1",
        title="t",
        company="Acme",
        location=location,
        description="d",
        content_fingerprint="fp",
    )


def _profile(locations: list[str], work_mode=WorkMode.ANY) -> CandidateProfile:
    return CandidateProfile(
        full_name="X",
        email="x@example.com",
        phone="1",
        preferred_locations=locations,
        work_mode=work_mode,
    )


def test_no_preferred_locations_gives_full_credit():
    settings = _settings()
    result = score_location(_job("Mumbai"), _profile([]), settings)
    assert result.points == result.max_points


def test_matching_location_gives_full_credit():
    settings = _settings()
    result = score_location(_job("Pune, India"), _profile(["Pune"]), settings)
    assert result.points == result.max_points


def test_non_matching_location_gives_zero():
    settings = _settings()
    result = score_location(_job("Mumbai"), _profile(["Pune"]), settings)
    assert result.points == 0


def test_remote_job_matches_remote_preference():
    settings = _settings()
    result = score_location(
        _job("Remote"), _profile(["Pune"], work_mode=WorkMode.REMOTE), settings
    )
    assert result.points == result.max_points


def test_remote_job_does_not_auto_match_onsite_only_preference():
    settings = _settings()
    result = score_location(
        _job("Remote"), _profile(["Pune"], work_mode=WorkMode.ONSITE), settings
    )
    assert result.points == 0
