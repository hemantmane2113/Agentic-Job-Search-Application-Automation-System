from pathlib import Path

import pytest
from pydantic import ValidationError

from naukri_agent.candidate.models import CandidateProfile, WorkMode, load_candidate_profile

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_valid_profile_from_yaml():
    profile = load_candidate_profile(FIXTURES / "candidate_profile_valid.yaml")
    assert profile.full_name == "Test Candidate"
    assert profile.work_mode == WorkMode.HYBRID
    assert profile.years_experience == 3.5


def test_skills_are_stripped_and_deduped():
    profile = load_candidate_profile(FIXTURES / "candidate_profile_valid.yaml")
    # "  SQL  " -> "SQL"; "Python" and "python" are distinct (case-sensitive)
    assert "SQL" in profile.skills
    assert profile.skills.count("Python") == 1


def test_missing_file_raises_actionable_error():
    with pytest.raises(FileNotFoundError, match="candidate_profile.example.yaml"):
        load_candidate_profile(FIXTURES / "does_not_exist.yaml")


def test_salary_max_below_min_rejected():
    with pytest.raises(ValidationError):
        CandidateProfile(
            full_name="X",
            email="x@example.com",
            phone="123",
            expected_salary_min_lpa=15,
            expected_salary_max_lpa=10,
        )


def test_negative_years_experience_rejected():
    with pytest.raises(ValidationError):
        CandidateProfile(
            full_name="X",
            email="x@example.com",
            phone="123",
            years_experience=-1,
        )


def test_invalid_email_rejected():
    with pytest.raises(ValidationError):
        CandidateProfile(full_name="X", email="not-an-email", phone="123")


def test_defaults_are_empty_not_none():
    profile = CandidateProfile(full_name="X", email="x@example.com", phone="123")
    assert profile.skills == []
    assert profile.work_mode == WorkMode.ANY
    assert profile.expected_salary_min_lpa is None
