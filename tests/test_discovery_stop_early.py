"""Discovery finds the day's 50 jobs with as few searches as it takes, instead of every role in every city."""

from __future__ import annotations

from naukri_agent.browser.models import JobDetail, JobListingSummary
from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.database.base import session_scope
from naukri_agent.database.models import RunEvent
from naukri_agent.orchestration.discovery import build_query_matrix, discover_and_store

from .digest_fakes import in_memory_factory, settings as _settings

ROLES = ["Data Scientist", "ML Engineer", "AI Engineer", "AI/ML Engineer"]
CITIES = ["Pune", "Mumbai", "Chennai", "Delhi", "Noida"]
GROUPS = {"data scientist": "ds", "ml engineer": "ml", "ai engineer": "ml", "ai/ml engineer": "ml"}


def profile():
    return CandidateProfile(full_name="X", email="x@example.com", phone="1", skills=["Python"], years_experience=3,
                            preferred_roles=ROLES, preferred_locations=CITIES)


def cfg(tmp_path, **over):
    base = dict(discovery_fresh_job_limit=50, discovery_freshness_days=7, discovery_max_total_jobs=200,
                resume_registry_path=tmp_path / "none.yaml")
    base.update(over)
    return _settings(tmp_path, **base)


class Client:
    """Every search returns `per_search` fresh cards that nobody else has; `stale_share` of them are old."""

    def __init__(self, per_search=20, stale_share=0.0):
        self.searches: list[tuple[str, str]] = []
        self.fetched: list[str] = []
        self.per_search, self.stale_share = per_search, stale_share

    def search_jobs(self, role, location=""):
        self.searches.append((role, location))
        n = len(self.searches)
        stale_from = int(self.per_search * (1 - self.stale_share))
        return [
            JobListingSummary(title="t", company="c", location="x", url=f"u/{n}/{i}",
                              posted_text="Just now" if i < stale_from else "30+ Days Ago")
            for i in range(self.per_search)
        ]

    def fetch_job_detail(self, url):
        self.fetched.append(url)
        return JobDetail(url=url, title="T", company="C", location="x", description=f"JD {url}")


def run(client, config):
    with session_scope(in_memory_factory()) as s:
        result, _ = discover_and_store(s, client, profile=profile(), settings=config, run_id=1, seq_start=0, role_groups=GROUPS)
        detail = dict(s.query(RunEvent).filter_by(stage="discover_freshness").one().detail)
    return result, detail


def test_the_role_only_searches_come_first_then_one_city_at_a_time(tmp_path):
    m = build_query_matrix(profile(), cfg(tmp_path))
    assert [(q.role, q.location) for q in m[:4]] == [(r, "") for r in ROLES]
    assert [(q.role, q.location) for q in m[4:8]] == [(r, "Pune") for r in ROLES]
    assert len(m) == 4 + 4 * 5


def test_searching_every_city_is_still_available_as_a_setting(tmp_path):
    m = build_query_matrix(profile(), cfg(tmp_path, discovery_search_every_city=True))
    assert len(m) == 20 and m[0].location == "Pune" and m[1].location == "Mumbai"


def test_it_stops_searching_once_fifty_fresh_jobs_are_found(tmp_path):
    client = Client(per_search=20)
    result, detail = run(client, cfg(tmp_path))
    assert len(client.fetched) == 50 and len(result.job_ids) == 50
    assert result.queries_run < 24 and detail["stopped_early"] is True
    assert detail["searches_run"] == result.queries_run and detail["searches_planned"] == 24
    assert all(loc == "" for _r, loc in client.searches[:4])


def test_every_resume_group_still_holds_an_equal_share_when_it_stops(tmp_path):
    # roles ds / ml / ml / ml: the three ml roles give 60 cards while ds gives 20 per search, so it keeps searching until ds has 25
    client = Client(per_search=20)
    run(client, cfg(tmp_path))
    ds = sum(1 for u in client.fetched if int(u.split("/")[1]) in {n for n, (r, _l) in enumerate(client.searches, 1) if r == "Data Scientist"})
    assert ds == 25 and len(client.fetched) - ds == 25


def test_when_the_cards_are_mostly_old_it_keeps_going_and_runs_out_of_searches(tmp_path):
    client = Client(per_search=20, stale_share=0.9)  # 2 fresh cards a search: 24 searches give ~48, short of 50
    result, detail = run(client, cfg(tmp_path))
    assert result.queries_run == 24 and detail["stopped_early"] is False and len(client.fetched) < 50


def test_an_explicit_query_list_is_run_in_full(tmp_path):
    client = Client(per_search=60)
    result, detail = run(client, cfg(tmp_path, discovery_queries=[f"{r} @ Pune" for r in ROLES] * 3))
    assert result.queries_run == 12 and detail["stopped_early"] is False


def test_the_old_every_city_mode_runs_every_search(tmp_path):
    client = Client(per_search=20)
    result, detail = run(client, cfg(tmp_path, discovery_search_every_city=True))
    assert result.queries_run == 20 and detail["stopped_early"] is False
