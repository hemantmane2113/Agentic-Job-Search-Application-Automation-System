"""One daily budget of 10 jobs: up to 4 through Telegram, the rest to apply yourself; plus the human-paced pauses."""

from __future__ import annotations

import datetime
import re

import pytest

from naukri_agent.browser.pacing import pause
from naukri_agent.config import Settings
from naukri_agent.database.base import session_scope
from naukri_agent.database.models import AutoApplyAttempt, Job
from naukri_agent.database.repositories import add_auto_apply_attempt
from naukri_agent.orchestration import discovery as discovery_mod
from naukri_agent.orchestration.pipeline import run_daily_recommendations
from naukri_agent.recommendations.builder import build_digest

from .digest_fakes import add_job, in_memory_factory, make_candidate, make_run, score, select_static_resume, settings
from .test_daily_pipeline import _VALID_EXTRACTION, _fake_discover, _PerJobLLM, _prep

NOW = datetime.datetime(2026, 9, 9, tzinfo=datetime.UTC)


def run_day(tmp_path, natives, companies, *, factory=None, **cfg_over):
    """A morning with `natives` Naukri-Apply jobs and `companies` apply-on-company-site jobs, all ACCEPT-level."""
    _prep(tmp_path)
    cfg = settings(tmp_path, dry_run=True, threshold_review=0, threshold_accept=0, **cfg_over)
    specs = [(f"n{i}", f"0409260{i:05d}", 0) for i in range(natives)] + [(f"c{i}", f"0509260{i:05d}", 0) for i in range(companies)]
    base = _fake_discover(specs)

    def discover(session, profile, settings_, run_id, seq):
        res, seq = base(session, profile, settings_, run_id, seq)
        for jid in res.job_ids:
            job = session.get(Job, jid)
            job.apply_type = "native" if "job-listings-n" in job.url else "company_site"
        return res, seq

    result = run_daily_recommendations(
        cfg, now=NOW, discover_fn=discover, extraction_provider=_PerJobLLM({}, _VALID_EXTRACTION),
        session_factory=factory or in_memory_factory(),
    )
    body = open(result.email_path, encoding="utf-8").read()
    part1_count = int(re.search(r"PART 1 .*?\((\d+)\)", body).group(1))
    return result, body, part1_count


# --- the split ----------------------------------------------------------------------------------------------------------------


def test_a_busy_day_is_four_through_telegram_and_six_to_apply_yourself(tmp_path):
    result, body, part1 = run_day(tmp_path, natives=8, companies=14)
    assert part1 == 6
    assert "Today's plan (at most 10 jobs): up to 4 through Telegram, up to 6 to apply yourself" in body
    assert "ready for Telegram (you can approve up to 4 today)" in body


def test_unused_telegram_slots_go_to_the_apply_yourself_list(tmp_path):
    _r, body, part1 = run_day(tmp_path, natives=1, companies=14)
    assert part1 == 9 and "up to 1 through Telegram, up to 9 to apply yourself" in body
    _r, body, part1 = run_day(tmp_path, natives=0, companies=14)
    assert part1 == 10 and "up to 0 through Telegram, up to 10 to apply yourself" in body


def test_the_total_never_goes_past_ten(tmp_path):
    for natives in (0, 2, 4, 9):
        _r, body, part1 = run_day(tmp_path, natives=natives, companies=20)
        telegram = int(re.search(r"up to (\d+) through Telegram", body).group(1))
        assert part1 + telegram <= 10


def test_a_quiet_day_lists_only_what_exists(tmp_path):
    _r, _body, part1 = run_day(tmp_path, natives=3, companies=2)
    assert part1 == 2


def test_telegram_slots_shrink_when_the_rolling_24_hours_already_used_them(tmp_path):
    f = in_memory_factory()
    with session_scope(f) as s:
        job = add_job(s, slug="old", ext="040926999999")
        for i in range(3):  # three applications within the last 24 hours
            row = add_auto_apply_attempt(s, job_id=job.id, attempt_id=f"a{i}", outcome="applied")
            row.attempted_at = datetime.datetime(2026, 9, 8, 18, 0)
    _r, body, part1 = run_day(tmp_path, natives=8, companies=14, factory=f)
    assert "up to 1 through Telegram, up to 9 to apply yourself" in body and part1 == 9


def test_when_the_whole_telegram_allowance_is_used_the_other_list_gets_the_full_ten(tmp_path):
    f = in_memory_factory()
    with session_scope(f) as s:
        job = add_job(s, slug="old", ext="040926999998")
        for i in range(4):
            row = add_auto_apply_attempt(s, job_id=job.id, attempt_id=f"b{i}", outcome="applied")
            row.attempted_at = datetime.datetime(2026, 9, 8, 20, 0)
    _r, _body, part1 = run_day(tmp_path, natives=8, companies=14, factory=f)
    assert part1 == 10


def test_the_budget_follows_the_settings(tmp_path):
    _r, body, part1 = run_day(tmp_path, natives=8, companies=14, daily_job_total=8, auto_apply_daily_cap=3)
    assert part1 == 5 and "at most 8 jobs): up to 3 through Telegram, up to 5" in body


def test_the_list_size_can_be_set_directly_on_the_digest_builder():
    f = in_memory_factory()
    c = settings(__import__("pathlib").Path("."), threshold_review=0, threshold_accept=0, recommendation_cooldown_days=0)
    with session_scope(f) as s:
        cand, run = make_candidate(s), make_run(s)
        ids = []
        for i in range(5):
            j = add_job(s, slug=f"j{i}", ext=f"04092610{i:04d}")
            score(s, cand.id, j.id, overall=90 - i)
            select_static_resume(s, j.id, cand.id)
            ids.append(j.id)
        def kw():  # each call is its own run: one recommendation row per job per run is recorded
            return dict(candidate_id=cand.id, candidate_email="c@example.com", scored_job_ids=ids, settings=c,
                        run_id=make_run(s).id, now=NOW)

        assert build_digest(s, **kw()).count == 5
        assert build_digest(s, **kw(), limit=2).count == 2
        d0 = build_digest(s, **kw(), limit=0)
        assert d0.count == 0 and d0.eligible_count == 5 and d0.truncated is True


# --- human-paced pauses ------------------------------------------------------------------------------------------------------------------


def test_defaults_are_the_agreed_budget_and_pauses():
    s = Settings(_env_file=None)
    assert (s.daily_job_total, s.auto_apply_daily_cap, s.daily_recommendation_limit) == (10, 4, 10)
    assert (s.browse_pause_min_seconds, s.browse_pause_max_seconds) == (3.0, 8.0)
    assert (s.apply_pause_min_seconds, s.apply_pause_max_seconds) == (2.0, 5.0)


def test_pause_sleeps_a_random_time_between_the_bounds():
    slept = []
    d = pause(3.0, 8.0, sleep=slept.append, uniform=lambda lo, hi: (lo + hi) / 2)
    assert d == 5.5 and slept == [5.5]
    seen = []
    pause(3.0, 8.0, sleep=lambda s: None, uniform=lambda lo, hi: seen.append((lo, hi)) or lo)
    assert seen == [(3.0, 8.0)]


@pytest.mark.parametrize("lo, hi", [(0, 0), (3, 0), (5, -1)])
def test_a_zero_maximum_turns_pausing_off(lo, hi):
    slept = []
    assert pause(lo, hi, sleep=slept.append) == 0.0 and slept == []


def test_inverted_bounds_do_not_crash():
    slept = []
    d = pause(10.0, 4.0, sleep=slept.append, uniform=lambda lo, hi: lo)
    assert d == 4.0  # min is clamped down to the max


def test_discovery_pauses_between_pages_but_not_before_the_first(monkeypatch, tmp_path):
    from naukri_agent.browser.models import JobDetail, JobListingSummary

    calls = []
    monkeypatch.setattr(discovery_mod, "pause", lambda lo, hi: calls.append((lo, hi)))

    class Client:
        def search_jobs(self, role, location=""):
            return [JobListingSummary(title="t", company="c", location="Pune", url=f"https://www.naukri.com/{role}-{location}-{i}", posted_text="Just now") for i in range(3)]

        def fetch_job_detail(self, url):
            return JobDetail(url=url, title="T", company="C", location="Pune", description=f"d {url}")

    cfg = settings(tmp_path, discovery_queries=["A@Pune", "B@Pune", "C@Pune"], browse_pause_min_seconds=3, browse_pause_max_seconds=8)
    f = in_memory_factory()
    with session_scope(f) as s:
        result, _ = discovery_mod.discover_and_store(s, Client(), profile=None, settings=cfg, run_id=1, seq_start=0, role_groups={})
    fetched = len(result.job_ids)
    assert len(calls) == (3 - 1) + (fetched - 1) and all(c == (3, 8) for c in calls)
