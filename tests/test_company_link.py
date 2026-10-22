"""The employer's direct job address, read from the job data Naukri's own page loads. It only looks: nothing is
pressed (pressing "Apply on company site" marks the job Applied on the account), and any failure only costs the link."""

from __future__ import annotations

import inspect
import json

from naukri_agent.browser import company_link
from naukri_agent.browser.company_link import CompanySiteLink, _address_in, _is_company_address, read_direct_apply_link
from naukri_agent.config import Settings
from naukri_agent.research_agent.models import ResearchReport
from naukri_agent.research_agent.runner import _with_direct_link, format_email_entry, format_report

from .browser_fakes import FakePage

JOB_URL = "https://www.naukri.com/job-listings-ai-engineer-lg-soft-061026504574"
JOB_API = "https://www.naukri.com/jobapi/v4/job/061026504574"
EMPLOYER = "https://lgsihrms.darwinbox.in/ms/candidatev2/a6914476a29263/careers/jobDetails/a6ac3648482f72"


def job_json(url=EMPLOYER, **extra):
    return json.dumps({"jobDetails": {"title": "AI Engineer", "applyRedirectUrl": url, **extra}}).encode()


class Page(FakePage):
    """Loading the job page makes Naukri's own page fetch its job data, as the real one does."""

    def __init__(self, responses=None, *, wait_hook=None):
        super().__init__()
        self.responses = responses if responses is not None else [("GET", JOB_API, job_json())]
        self.removed = []
        self.wait_hook = wait_hook

        def loaded(url):
            for method, resp_url, body in self.responses:
                self.simulate_response(method, resp_url, headers={"content-type": "application/json"}, body=body)

        self.on_goto = loaded

    def remove_listener(self, event, handler):
        self.removed.append(event)
        self.event_listeners[event].remove(handler)

    def wait_for_timeout(self, ms):
        super().wait_for_timeout(ms)
        if self.wait_hook:
            self.wait_hook()


def test_the_address_in_the_job_data_is_read():
    page = Page()
    link = read_direct_apply_link(page, JOB_URL)
    assert link.url == EMPLOYER and "nothing was pressed" in link.note
    assert page.goto_calls == [JOB_URL]


def test_nothing_is_ever_pressed_typed_or_sent():
    page = Page()
    read_direct_apply_link(page, JOB_URL)
    assert page.clicked == [] and page.filled == {} and page.routes == []  # no click, no typing, no request guard needed
    assert page.requests == []  # the page sent nothing of its own


def test_the_module_has_no_way_to_press_anything():
    src = inspect.getsource(company_link)
    for word in (".click(", ".fill(", "COMPANY_SITE_APPLY_BUTTON", "APPLY_BUTTON", "MutatingRequestBlocker"):
        assert word not in src


def test_the_listener_is_removed_afterwards():
    page = Page()
    read_direct_apply_link(page, JOB_URL)
    assert page.removed == ["response"] and page.event_listeners["response"] == []


def test_other_responses_and_methods_are_ignored():
    page = Page([
        ("GET", "https://www.naukri.com/jobapi/v2/search/simjobs/1", job_json("https://other.example/similar-job")),
        ("POST", JOB_API, job_json("https://post.example/x")),
        ("GET", JOB_API, job_json()),
    ])
    assert read_direct_apply_link(page, JOB_URL).url == EMPLOYER


def test_a_job_with_no_company_address_says_so():
    page = Page([("GET", JOB_API, json.dumps({"jobDetails": {"title": "x"}}).encode())])
    link = read_direct_apply_link(page, JOB_URL)
    assert link.url is None and "holds no company address" in link.note


def test_an_address_inside_naukri_is_never_taken_for_the_employers():
    page = Page([("GET", JOB_API, job_json("https://www.naukri.com/some/redirect"))])
    assert read_direct_apply_link(page, JOB_URL).url is None


def test_when_the_data_never_loads_it_waits_then_says_so():
    page = Page([])
    link = read_direct_apply_link(page, JOB_URL, wait_ms=900)
    assert link.url is None and "did not load" in link.note and sum(page.wait_for_timeout_calls) >= 900


def test_it_stops_waiting_as_soon_as_the_data_has_arrived():
    page = Page()
    read_direct_apply_link(page, JOB_URL)
    assert page.wait_for_timeout_calls == []


def test_a_response_that_cannot_be_read_does_not_break_the_page_visit():
    page = Page([])
    page.on_goto = lambda url: page.simulate_response("GET", JOB_API, body_error=RuntimeError("closed"))
    assert read_direct_apply_link(page, JOB_URL, wait_ms=300).url is None


def test_which_addresses_count():
    assert _is_company_address("https://acme.example/jobs/1")
    for bad in (None, "", "about:blank", "javascript:1", "https://naukri.com/x", "https://www.naukri.com/x", "https://logs.naukri.com/x", 5):
        assert not _is_company_address(bad)
    assert _address_in(b"not json") is None and _address_in(None) is None and _address_in(b"[]") is None


# --- in the research report and the email ---------------------------------------------------------------------------


JOB = {"job_id": 1, "title": "AI Engineer (HS_Kitchen)", "company": "LG Soft India", "location": "Bengaluru", "url": JOB_URL}
REPORT = ResearchReport(company_summary="s", careers_url="https://lgsoftindia.com/careers")


class Reader:
    def __init__(self, link=None, error=None):
        self.link, self.error, self.asked = link, error, []

    def direct_apply_link(self, url):
        self.asked.append(url)
        if self.error:
            raise self.error
        return self.link


def cfg():
    return Settings(_env_file=None, browse_pause_min_seconds=0, browse_pause_max_seconds=0)


def test_the_link_goes_into_the_report_and_is_the_first_thing_in_the_email():
    out = _with_direct_link(REPORT, Reader(CompanySiteLink(EMPLOYER, "ok")), JOB, cfg())
    assert out.direct_link == EMPLOYER
    entry = format_email_entry(1, JOB, out)
    assert entry.splitlines()[1] == f"   Direct apply link: {EMPLOYER}"
    assert f"Direct apply link: {EMPLOYER}" in format_report(JOB, out)


def test_without_a_link_the_email_does_not_pretend_and_says_why():
    out = _with_direct_link(REPORT, Reader(CompanySiteLink(None, "Naukri's job data holds no company address for this job")), JOB, cfg())
    assert out.direct_link is None
    entry = format_email_entry(1, JOB, out)
    assert "Direct apply link" not in entry and "direct link not read: Naukri's job data holds no company address" in entry


def test_a_browser_failure_only_costs_the_link():
    out = _with_direct_link(REPORT, Reader(error=RuntimeError("page closed")), JOB, cfg())
    assert out.direct_link is None and out.company_summary == "s"
    assert "direct link not read (RuntimeError)" in out.notes


def test_reading_the_link_is_on_by_default_because_it_changes_nothing():
    assert Settings(_env_file=None).research_read_direct_link is True


def test_the_model_cannot_supply_the_address():
    from naukri_agent.research_agent.loop import finalize_report
    from naukri_agent.research_agent.tools import ToolBox

    box = ToolBox.__new__(ToolBox)
    box.seen, box.fetched = set(), []
    claimed = ResearchReport(company_summary="s", direct_link="https://evil.example/apply")
    assert finalize_report(claimed, box).direct_link is None


def test_the_search_hint_is_dropped_when_the_direct_link_was_found():
    thin = REPORT.model_copy(update={"find_by_title": True})
    assert "Finding the job" in format_email_entry(1, JOB, thin)
    with_link = thin.model_copy(update={"direct_link": EMPLOYER})
    assert "Finding the job" not in format_email_entry(1, JOB, with_link)
    assert "Finding the job" not in format_report(JOB, with_link)
