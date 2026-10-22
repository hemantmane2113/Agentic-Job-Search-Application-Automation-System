"""Keep full-time permanent jobs; leave out contract / temporary / part-time ones, using Naukri's OWN
Employment Type line (never the AI's guess, which was wrong for 6 of 8 sampled 'contract' jobs)."""

from __future__ import annotations

import datetime

import pytest

from naukri_agent.browser.jobs import read_employment_type
from naukri_agent.database.base import session_scope
from naukri_agent.database.models import AutoApplyAttempt, Job
from naukri_agent.database.repositories import upsert_job
from naukri_agent.jobs.models import JobCreate
from naukri_agent.notifications.render import render_digest
from naukri_agent.orchestration.pipeline import _apply_ready_text
from naukri_agent.orchestration.telegram_interaction import format_job_card
from naukri_agent.recommendations.apply_ready import select_candidates
from naukri_agent.recommendations.builder import build_digest
from naukri_agent.recommendations.employment import EXCLUDED, OK, UNCONFIRMED, classify_employment, describe, is_excluded
from naukri_agent.research_agent.runner import select_jobs

from .digest_fakes import add_job, in_memory_factory, make_candidate, make_run, score, select_static_resume, settings
from .test_auto_apply_interactive import FakeHuman, go
from .test_auto_apply_runner import FakeClient, _patch_profile, cfg, seed  # noqa: F401 - autouse fixture
from .test_telegram import JOB

NOW = datetime.datetime(2026, 10, 9, 5, 0, tzinfo=datetime.UTC)

# --- the rule -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Full Time, Permanent", OK),
        ("Full-Time", OK),
        ("FULL TIME, PERMANENT", OK),
        ("Permanent", OK),
        ("Full Time, Temporary/Contractual", EXCLUDED),
        ("Contract", EXCLUDED),
        ("Contractual", EXCLUDED),
        ("Temporary", EXCLUDED),
        ("Freelance", EXCLUDED),
        ("Part Time, Permanent", EXCLUDED),
        ("Part-time", EXCLUDED),
        ("Internship", EXCLUDED),
        ("Apprenticeship", EXCLUDED),
        ("Volunteer", EXCLUDED),
        (None, UNCONFIRMED),
        ("", UNCONFIRMED),
        ("   ", UNCONFIRMED),
        ("Walk-in", UNCONFIRMED),
    ],
)
def test_classification(text, expected):
    assert classify_employment(text) == expected


def test_the_switch_turns_the_filter_off():
    assert is_excluded("Contract", enabled=True) is True
    assert is_excluded("Contract", enabled=False) is False
    assert is_excluded(None) is False and is_excluded("Full Time, Permanent") is False


def test_messages_say_not_confirmed_rather_than_guessing():
    assert describe(None) == "not confirmed" and describe("Walk-in") == "not confirmed"
    assert describe("Full Time, Permanent") == "Full Time, Permanent"


# --- reading and storing it ------------------------------------------------------------------------------------------


class Page:
    def __init__(self, value=None, boom=False):
        self.value, self.boom = value, boom

    def evaluate(self, script, arg=None):
        if self.boom:
            raise RuntimeError("page closed")
        return self.value


def test_the_employment_type_is_read_from_the_page_and_misses_degrade_to_none():
    assert read_employment_type(Page("  Full Time,   Permanent ")) == "Full Time, Permanent"
    assert read_employment_type(Page(None)) is None and read_employment_type(Page("  ")) is None
    assert read_employment_type(Page(42)) is None and read_employment_type(Page(boom=True)) is None


def test_the_stored_value_is_updated_but_a_missed_read_never_erases_it():
    f = in_memory_factory()
    base = dict(title="DS", company="Acme", location="Pune", description="d", url="https://www.naukri.com/job-listings-ds-040926000001")
    with session_scope(f) as s:
        job, _ = upsert_job(s, JobCreate(**base, employment_type_text="Full Time, Permanent"))
        upsert_job(s, JobCreate(**base))  # this time the page showed none
        assert s.get(Job, job.id).employment_type_text == "Full Time, Permanent"
        upsert_job(s, JobCreate(**base, employment_type_text="Contract"))  # Naukri changed it
        assert s.get(Job, job.id).employment_type_text == "Contract"


# --- the email digest ------------------------------------------------------------------------------------------------


def digest_for(types, **cfg_over):
    """One recommendation-worthy job per entry in `types` (an Employment Type string or None)."""
    f = in_memory_factory()
    c = settings(__import__("pathlib").Path("."), threshold_review=0, threshold_accept=0, **cfg_over)
    with session_scope(f) as s:
        cand, run = make_candidate(s), make_run(s)
        ids = []
        for i, t in enumerate(types):
            j = add_job(s, slug=f"j{i}", ext=f"04092600{i:04d}", title=f"role{i}")
            j.apply_type = "company_site"
            j.employment_type_text = t
            score(s, cand.id, j.id, overall=90 - i)
            select_static_resume(s, j.id, cand.id)
            ids.append(j.id)
        d = build_digest(s, candidate_id=cand.id, candidate_email="c@example.com", scored_job_ids=ids,
                         settings=c, run_id=run.id, now=NOW, manual_apply_only=True)
        return d, render_digest(d, c).text_body


def test_contract_temporary_and_part_time_jobs_are_left_out_and_counted():
    d, body = digest_for(["Full Time, Permanent", "Full Time, Temporary/Contractual", "Part Time", "Contract"])
    assert [r.job_title for r in d.recommendations] == ["role0"] and d.type_excluded == 3
    assert "Left out: 3 contract, temporary or part-time job(s)" in body
    assert "role1" not in body and "role3" not in body


def test_a_job_with_no_employment_type_is_kept_and_marked_not_confirmed():
    d, body = digest_for([None, "Full Time, Permanent"])
    assert len(d.recommendations) == 2 and d.type_excluded == 0
    assert "Job type: not confirmed" in body and "Job type: Full Time, Permanent" in body
    assert "Left out" not in body


def test_the_filter_can_be_switched_off():
    d, body = digest_for(["Contract", "Full Time, Permanent"], employment_filter_enabled=False)
    assert len(d.recommendations) == 2 and d.type_excluded == 0 and "Left out" not in body


def test_only_jobs_that_would_have_been_recommended_are_counted_as_left_out():
    f = in_memory_factory()
    c = settings(__import__("pathlib").Path("."), threshold_review=0, threshold_accept=95, recommendation_min_score=70)
    with session_scope(f) as s:
        cand, run = make_candidate(s), make_run(s)
        j = add_job(s, slug="low", ext="040926009999")
        j.employment_type_text = "Contract"
        score(s, cand.id, j.id, overall=40.0)  # below the bar anyway
        d = build_digest(s, candidate_id=cand.id, candidate_email="c@example.com", scored_job_ids=[j.id], settings=c, run_id=run.id, now=NOW)
    assert d.type_excluded == 0


# --- the apply-ready list, the phone card and the ping --------------------------------------------------------------------------


def set_types(factory, urls, types):
    with session_scope(factory) as s:
        for slug, t in types.items():
            s.query(Job).filter_by(url=urls[slug]).one().employment_type_text = t


def test_excluded_jobs_are_never_offered_for_applying_and_unknown_ones_are(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("perm", "040926003001", 95.0, {}), ("contract", "040926003002", 94.0, {}), ("unknown", "040926003003", 93.0, {})])
    set_types(factory, urls, {"perm": "Full Time, Permanent", "contract": "Full Time, Temporary/Contractual", "unknown": None})
    from naukri_agent.database.models import Candidate

    with session_scope(factory) as s:
        picked = select_candidates(s, s.query(Candidate).one().id, c, datetime.datetime(2026, 10, 8, 12, 0, tzinfo=datetime.UTC))
    assert [j["title"] for j in picked] == ["perm role", "unknown role"]
    assert picked[0]["employment_type_text"] == "Full Time, Permanent" and picked[1]["employment_type_text"] is None


def test_the_job_card_and_the_ping_show_the_type():
    assert "Job type: Full Time, Permanent" in format_job_card({**JOB, "employment_type_text": "Full Time, Permanent"})
    assert "Job type: not confirmed" in format_job_card({**JOB, "employment_type_text": None})
    text = _apply_ready_text([{"title": "A", "company": "X", "score": 91.0, "resume_id": "r", "employment_type_text": None}], 3, 3)
    assert "not confirmed" in text


def test_the_live_page_check_stops_a_contract_job_the_database_did_not_know_about(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("late", "040926004001", 95.0, {})])  # stored type unknown: it passes the list
    fake = FakeClient({urls["late"]: {"employment": "Full Time, Temporary/Contractual"}})
    human = FakeHuman(approvals=[True])
    r = go(c, factory, fake, human)
    assert r.applied == 0 and r.outcomes[0].outcome == "excluded_type" and "Naukri says" in r.outcomes[0].detail
    assert fake.clicked == [] and fake.submitted_urls == []
    assert any("not full-time permanent" in n for n in human.notes)
    with session_scope(factory) as s:
        assert s.query(AutoApplyAttempt).one().outcome == "excluded_type"


def test_a_skipped_contract_job_is_not_offered_again_for_30_days(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("late", "040926004101", 95.0, {})])
    go(c, factory, FakeClient({urls["late"]: {"employment": "Contract"}}), FakeHuman(approvals=[True]))
    again = FakeClient({urls["late"]: {}})
    r = go(c, factory, again, FakeHuman(approvals=[True]))
    assert r.candidates_considered == 0 and again.opened == []


def test_a_page_that_shows_no_type_does_not_block_applying(tmp_path):
    c = cfg(tmp_path)
    factory, urls = seed(c, [("ok", "040926004201", 95.0, {})])
    r = go(c, factory, FakeClient({urls["ok"]: {}}), FakeHuman(approvals=[True]))
    assert r.applied == 1


# --- the researcher's list ----------------------------------------------------------------------------------------------------


def test_the_researcher_skips_contract_jobs(tmp_path):
    from naukri_agent.database.models import DailyRunStatus, JobRecommendation
    from naukri_agent.matching.models import MatchDecision

    f = in_memory_factory()
    c = settings(tmp_path)
    with session_scope(f) as s:
        cand, run = make_candidate(s), make_run(s)
        run.status = DailyRunStatus.COMPLETED
        for rank, (slug, t) in enumerate([("perm", "Full Time, Permanent"), ("contract", "Contract"), ("unknown", None)], 1):
            j = add_job(s, slug=slug, ext=f"04092600{rank:04d}", title=f"{slug} role")
            j.apply_type = "company_site"
            j.employment_type_text = t
            s.add(JobRecommendation(candidate_id=cand.id, job_id=j.id, daily_run_id=run.id, rank=rank,
                                    score_at_email=90.0, decision_at_email=MatchDecision.ACCEPT))
        s.flush()
        picked = select_jobs(s, c, NOW, job_id=None, limit=5)
    assert [j["title"] for j in picked] == ["perm role", "unknown role"]


# --- the title rule (only when the page shows no Employment Type) -----------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    ["Data Scientist (Freelancer)", "Freelance Data Scientist", "Data Science Intern", "Data Scientist - Internship",
     "Contract Data Scientist", "Data Scientist (Contractual)", "Temporary Data Analyst", "Data Scientist - Part Time"],
)
def test_a_title_that_says_contract_freelance_or_intern_counts_when_the_page_shows_no_type(title):
    assert is_excluded(None, True, title=title) is True


@pytest.mark.parametrize("title", ["Data Scientist", "Senior Data Scientist, Actimize", "International Data Scientist", "Internal Tools Engineer", "Machine Learning Engineer II"])
def test_ordinary_titles_are_never_excluded_by_the_title_rule(title):
    assert is_excluded(None, True, title=title) is False


def test_naukris_own_type_beats_the_title_when_it_exists():
    assert is_excluded("Full Time, Permanent", True, title="Contract Management Data Scientist") is False  # page wins
    assert is_excluded("Contract", True, title="Data Scientist") is True
    assert is_excluded(None, False, title="Data Scientist (Freelancer)") is False  # switch off


def test_the_title_rule_reaches_the_digest_and_the_apply_list(tmp_path):
    d, body = digest_for([None, None])
    assert len(d.recommendations) == 2  # untitled-type jobs with ordinary titles are kept
    f = in_memory_factory()
    c = settings(__import__("pathlib").Path("."), threshold_review=0, threshold_accept=0)
    with session_scope(f) as s:
        cand, run = make_candidate(s), make_run(s)
        j = add_job(s, slug="fl", ext="040926007777", title="Data Scientist (Freelancer)")
        score(s, cand.id, j.id, overall=90.0)
        dg = build_digest(s, candidate_id=cand.id, candidate_email="c@example.com", scored_job_ids=[j.id], settings=c, run_id=run.id, now=NOW)
    assert dg.count == 0 and dg.type_excluded == 1
