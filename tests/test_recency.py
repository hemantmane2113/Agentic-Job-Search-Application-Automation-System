"""Posted recency: 7 marks for a job posted today, down to 1 for a week old; the agreed distribution adds up to 100."""

from __future__ import annotations

import datetime

import pytest

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.config import Settings
from naukri_agent.database.models import Job, JobExtraction
from naukri_agent.matching.recency_matcher import posted_age_days, recency_steps, score_recency
from naukri_agent.matching.scorer import score_job
from naukri_agent.resume.models import MasterResume

NOW = datetime.datetime(2026, 10, 9, 5, 0, tzinfo=datetime.UTC)


def job(posted):
    return Job(url="https://example.com/1", title="Data Scientist", company="Acme", location="Pune",
               description="d", content_fingerprint="fp", posted_date_text=posted)


def iso_days_ago(n):
    return (NOW.date() - datetime.timedelta(days=n)).isoformat()


# --- the marks --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("age, marks", [(0, 7), (1, 6), (2, 5), (3, 4), (4, 3), (5, 2), (6, 1), (7, 1)])
def test_the_marks_fall_from_seven_to_one_over_a_week(age, marks):
    cs = score_recency(job(iso_days_ago(age)), Settings(_env_file=None), NOW)
    assert cs.points == marks and cs.max_points == 7


@pytest.mark.parametrize("age", [8, 9, 30])
def test_older_than_a_week_earns_nothing(age):
    cs = score_recency(job(iso_days_ago(age)), Settings(_env_file=None), NOW)
    assert cs.points == 0 and cs.max_points == 7 and f"Posted {age} days ago" in cs.negative_factors[0]


@pytest.mark.parametrize("posted", [None, "", "   ", "sometime", "Reposted"])
def test_an_unreadable_date_earns_nothing_and_says_why(posted):
    cs = score_recency(job(posted), Settings(_env_file=None), NOW)
    assert cs.points == 0 and cs.negative_factors == ["Posted date could not be read"]


def test_the_reason_is_written_for_the_email():
    assert score_recency(job(iso_days_ago(0)), Settings(_env_file=None), NOW).positive_factors == ["Posted today"]
    assert score_recency(job(iso_days_ago(1)), Settings(_env_file=None), NOW).positive_factors == ["Posted 1 day ago"]
    assert score_recency(job(iso_days_ago(4)), Settings(_env_file=None), NOW).positive_factors == ["Posted 4 days ago"]


def test_the_marks_scale_with_the_weight():
    assert score_recency(job(iso_days_ago(0)), Settings(_env_file=None, weight_recency=14), NOW).points == 14
    assert score_recency(job(iso_days_ago(3)), Settings(_env_file=None, weight_recency=14), NOW).points == 8
    assert score_recency(job(iso_days_ago(0)), Settings(_env_file=None, weight_recency=0), NOW).points == 0


# --- reading the posting date ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, age",
    [
        ("2026-10-09", 0), ("2026-10-08", 1), ("2026-10-02T09:15:00+05:30", 7), ("2026-09-15", 24),
        ("Just now", 0), ("Today", 0), ("Few hours ago", 0), ("5 hours ago", 0), ("30 minutes ago", 0),
        ("Yesterday", 1), ("1 Day Ago", 1), ("3 Days Ago", 3), ("30+ Days Ago", 30), ("2 weeks ago", 14),
        ("2026-10-12", 0),  # a date in the future never gives a negative age
    ],
)
def test_posting_dates_are_read_in_every_form_naukri_uses(text, age):
    assert posted_age_days(text, NOW) == age


@pytest.mark.parametrize("text", [None, "", "  ", "unknown", "2026-13-45"])
def test_unreadable_dates_give_none(text):
    assert posted_age_days(text, NOW) is None


def test_recency_steps_boundaries():
    assert [recency_steps(a) for a in (None, 0, 1, 6, 7, 8)] == [0, 7, 6, 1, 1, 0]


# --- the agreed distribution -------------------------------------------------------------------------------------------------


def test_the_agreed_distribution_adds_up_to_one_hundred():
    s = Settings(_env_file=None)
    weights = {"skills": s.weight_skills, "experience": s.weight_experience, "role": s.weight_role,
               "salary": s.weight_salary, "location": s.weight_location, "education": s.weight_education,
               "recency": s.weight_recency}
    assert weights == {"skills": 35, "experience": 20, "role": 15, "salary": 11, "location": 7, "education": 5, "recency": 7}
    assert sum(weights.values()) == 100


def profile():
    return CandidateProfile(full_name="X", email="x@example.com", phone="1", skills=["Python", "SQL"], years_experience=3,
                            preferred_roles=["Data Scientist"], preferred_locations=["Pune"], expected_salary_min_lpa=8)


def extraction():
    return JobExtraction(normalized_title="Data Scientist", required_skills=["Python", "SQL"], preferred_skills=[],
                         experience_min=2, experience_max=5, salary_max=14)


def score(posted, now=NOW):
    return score_job(job(posted), extraction(), profile(), MasterResume(professional_summary="s"), Settings(_env_file=None), now=now)


def test_recency_is_one_of_seven_categories_and_the_maximum_is_one_hundred():
    r = score(iso_days_ago(0))
    assert set(r.category_scores) == {"skills", "experience", "role", "salary", "location", "education", "recency"}
    assert sum(cs.max_points for cs in r.category_scores.values()) == 100


def test_the_same_job_scores_higher_the_fresher_it_is_by_exactly_the_recency_marks():
    today, three_days, week = score(iso_days_ago(0)), score(iso_days_ago(3)), score(iso_days_ago(6))
    assert round(today.overall_score - three_days.overall_score, 1) == 3.0
    assert round(today.overall_score - week.overall_score, 1) == 6.0
    assert score(iso_days_ago(20)).overall_score < week.overall_score  # past a week: no marks at all


def test_the_run_time_is_what_the_age_is_measured_against():
    posted = "2026-10-01"
    early = score(posted, now=datetime.datetime(2026, 10, 2, 5, 0, tzinfo=datetime.UTC))  # 1 day old: 6 marks
    late = score(posted, now=datetime.datetime(2026, 10, 8, 5, 0, tzinfo=datetime.UTC))   # 7 days old: 1 mark
    assert round(early.overall_score - late.overall_score, 1) == 5.0


def test_recency_can_tip_a_job_over_the_accept_line_but_only_by_its_own_marks():
    fresh, old = score(iso_days_ago(0)), score(iso_days_ago(6))
    bar = (fresh.overall_score + old.overall_score) / 2     # a bar between the two
    s = Settings(_env_file=None, threshold_accept=bar, threshold_review=bar - 10)
    args = (profile(), MasterResume(professional_summary="s"), s)
    assert score_job(job(iso_days_ago(0)), extraction(), *args, now=NOW).decision.value == "ACCEPT"
    assert score_job(job(iso_days_ago(6)), extraction(), *args, now=NOW).decision.value == "REVIEW"
