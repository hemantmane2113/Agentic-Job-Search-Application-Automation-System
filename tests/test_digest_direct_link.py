"""The employer's direct job link, read during the daily run's visit to each job page, stored on the job, and shown
in Part 1 of the digest, so it is in the first email and not only in the research email."""

from __future__ import annotations

import datetime
import json

from naukri_agent.browser import jobs as jobs_module
from naukri_agent.browser.models import JobDetail
from naukri_agent.database.base import session_scope
from naukri_agent.database.models import Job
from naukri_agent.database.repositories import upsert_job
from naukri_agent.jobs.models import JobCreate
from naukri_agent.notifications.render import render_digest
from naukri_agent.orchestration.discovery import discover_and_store
from naukri_agent.recommendations.builder import build_digest
from naukri_agent.research_agent.models import ResearchReport
from naukri_agent.research_agent.runner import _with_direct_link

from .browser_fakes import FakePage
from .digest_fakes import add_job, in_memory_factory, make_candidate, make_run, score, select_static_resume, settings

URL = "https://www.naukri.com/job-listings-ai-engineer-lg-soft-061026504574"
API = "https://www.naukri.com/jobapi/v4/job/061026504574"
EMPLOYER = "https://lgsihrms.darwinbox.in/ms/candidatev2/a6914476a29263/careers/jobDetails/a6ac3648482f72"
NOW = datetime.datetime(2026, 10, 9, 5, 0, tzinfo=datetime.UTC)


def job_json(url=EMPLOYER):
    return json.dumps({"jobDetails": {"applyRedirectUrl": url}}).encode()


class Page(FakePage):
    def __init__(self, body=None, method="GET"):
        super().__init__()
        self.removed = []
        self.on_goto = lambda url: self.simulate_response(method, API, body=job_json() if body is None else body)

    def remove_listener(self, event, handler):
        self.removed.append(event)
        self.event_listeners[event].remove(handler)


# --- reading it on the daily run's page visit ------------------------------------------------------------------------


def test_the_job_page_read_keeps_the_employers_address():
    page = Page()
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.apply_redirect_url == EMPLOYER
    assert page.clicked == [] and page.filled == {} and page.removed == ["response"]


def test_a_job_with_no_company_address_has_none():
    assert jobs_module.fetch_job_detail(Page(body=json.dumps({"jobDetails": {}}).encode()), URL).apply_redirect_url is None


def test_a_naukri_address_is_not_kept():
    assert jobs_module.fetch_job_detail(Page(body=job_json("https://www.naukri.com/x")), URL).apply_redirect_url is None


def test_a_failed_page_open_leaves_no_listener_behind_and_no_link():
    page = Page()
    page.on_goto = lambda url: (_ for _ in ()).throw(RuntimeError("net::ERR"))
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.apply_redirect_url is None and page.removed == ["response"]


# --- stored on the job -------------------------------------------------------------------------------------------------


def _job(url=None):
    return JobCreate(title="AI Engineer", company="LG", location="Bengaluru", description="d", url=URL,
                     apply_type="company_site", apply_redirect_url=url)


def test_the_address_is_stored_and_a_later_visit_that_could_not_read_it_does_not_erase_it():
    with session_scope(in_memory_factory()) as s:
        job, _ = upsert_job(s, _job(EMPLOYER))
        assert job.apply_redirect_url == EMPLOYER
        again, _ = upsert_job(s, _job(None))
        assert again.id == job.id and again.apply_redirect_url == EMPLOYER
        changed, _ = upsert_job(s, _job("https://acme.example/jobs/2"))
        assert changed.apply_redirect_url == "https://acme.example/jobs/2"


class Client:
    def __init__(self, link):
        self.link = link

    def search_jobs(self, role, location=""):
        from naukri_agent.browser.models import JobListingSummary

        return [JobListingSummary(title="t", company="c", location="x", url=URL, posted_text="Just now")]

    def fetch_job_detail(self, url):
        return JobDetail(url=url, title="AI Engineer", company="LG", location="Bengaluru", description="d",
                         apply_type="company_site", apply_redirect_url=self.link)


def test_discovery_stores_the_address_it_was_given(tmp_path):
    cfg = settings(tmp_path, discovery_queries=["AI Engineer"], resume_registry_path=tmp_path / "none.yaml")
    with session_scope(in_memory_factory()) as s:
        discover_and_store(s, Client(EMPLOYER), profile=None, settings=cfg, run_id=1, seq_start=0)
        assert s.query(Job).one().apply_redirect_url == EMPLOYER


# --- in the digest ------------------------------------------------------------------------------------------------------


def _digest_text(tmp_path, link, apply_type="company_site"):
    cfg = settings(tmp_path)
    with session_scope(in_memory_factory()) as s:
        cand, run = make_candidate(s), make_run(s)
        job = add_job(s, slug="lg", ext="061026504574", title="AI Engineer", company="LG Soft India")
        job.apply_type, job.apply_redirect_url = apply_type, link
        score(s, cand.id, job.id, overall=90.0)
        select_static_resume(s, job.id, cand.id)
        d = build_digest(s, candidate_id=cand.id, candidate_email="c@example.com", scored_job_ids=[job.id],
                         settings=cfg, run_id=run.id, now=NOW)
        return render_digest(d, cfg).text_body


def test_part_one_shows_the_direct_apply_link(tmp_path):
    body = _digest_text(tmp_path, EMPLOYER)
    assert f"Direct apply link: {EMPLOYER}" in body and "ON THE COMPANY'S WEBSITE" in body


def test_part_one_without_a_stored_address_looks_as_it_did_before(tmp_path):
    body = _digest_text(tmp_path, None)
    assert "Direct apply link" not in body and "use the link below, then run mark-applied" in body


def test_a_naukri_apply_job_never_shows_a_direct_link_line(tmp_path):
    assert "Direct apply link" not in _digest_text(tmp_path, EMPLOYER, apply_type="native")


# --- the research step reuses it ---------------------------------------------------------------------------------------


class Reader:
    def __init__(self):
        self.asked = []

    def direct_apply_link(self, url):
        self.asked.append(url)
        raise AssertionError("the page should not be opened again")


def test_the_research_step_uses_the_stored_address_and_does_not_open_the_page_again():
    from naukri_agent.config import Settings

    reader = Reader()
    job = {"job_id": 1, "title": "t", "company": "c", "location": "x", "url": URL, "apply_redirect_url": EMPLOYER}
    out = _with_direct_link(ResearchReport(company_summary="s"), reader, job, Settings(_env_file=None))
    assert out.direct_link == EMPLOYER and reader.asked == []
