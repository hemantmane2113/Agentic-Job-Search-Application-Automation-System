import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from naukri_agent.resume.models import MasterResume, WorkExperience, load_master_resume

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_valid_resume_from_yaml():
    resume = load_master_resume(FIXTURES / "master_resume_valid.yaml")
    assert resume.professional_summary == "Test summary."
    assert len(resume.work_experience) == 2


def test_total_years_experience_sums_date_ranges():
    resume = load_master_resume(FIXTURES / "master_resume_valid.yaml")
    # Company A: exactly 1 year, Company B: exactly 2 years -> 3.0 total
    assert resume.total_years_experience() == pytest.approx(3.0, abs=0.05)


def test_total_years_experience_handles_current_role():
    resume = MasterResume(
        professional_summary="s",
        work_experience=[
            WorkExperience(
                company="C",
                title="T",
                start_date=datetime.date.today() - datetime.timedelta(days=365),
                end_date=None,  # current role -> counts up to today
            )
        ],
    )
    assert resume.total_years_experience() == pytest.approx(1.0, abs=0.05)


def test_content_hash_is_stable_and_changes_with_content():
    resume_a = load_master_resume(FIXTURES / "master_resume_valid.yaml")
    resume_b = load_master_resume(FIXTURES / "master_resume_valid.yaml")
    assert resume_a.content_hash() == resume_b.content_hash()

    resume_c = resume_a.model_copy(update={"professional_summary": "Different."})
    assert resume_c.content_hash() != resume_a.content_hash()


def test_missing_file_raises_actionable_error():
    with pytest.raises(FileNotFoundError, match="master_resume.example.yaml"):
        load_master_resume(FIXTURES / "does_not_exist.yaml")


def test_end_date_before_start_date_rejected():
    with pytest.raises(ValidationError):
        WorkExperience(
            company="C",
            title="T",
            start_date=datetime.date(2024, 1, 1),
            end_date=datetime.date(2023, 1, 1),
        )


def test_is_current_property():
    current = WorkExperience(company="C", title="T", start_date=datetime.date(2024, 1, 1))
    past = WorkExperience(
        company="C", title="T", start_date=datetime.date(2020, 1, 1), end_date=datetime.date(2021, 1, 1)
    )
    assert current.is_current is True
    assert past.is_current is False
