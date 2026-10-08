"""LinkedIn L1 read-only inspection: behaviour against a fake browser (no network, no Playwright)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from naukri_agent.browser import selectors as sel
from naukri_agent.browser.linkedin_inspection import (
    JOB_PROBE_JS,
    SEARCH_PROBE_JS,
    ReadOnlyGuard,
    run_linkedin_inspection,
)
from naukri_agent.config import Settings

FEED = sel.LINKEDIN_FEED_URL


class FakeRoute:
    def __init__(self, method, url):
        self.request = SimpleNamespace(method=method, url=url)
        self.action = None

    def abort(self):
        self.action = "abort"

    def continue_(self):
        self.action = "continue"


class FakeContext:
    def __init__(self):
        self.handler = None

    def route(self, pattern, handler):
        assert pattern == "**/*"
        self.handler = handler


class FakePage:
    """`redirects` maps a URL to what the page ends up on (e.g. feed -> /login while logged out)."""

    def __init__(self, redirects=None, search_probe=None, job_probe=None):
        self.url = "about:blank"
        self.visited = []
        self.redirects = dict(redirects or {})
        self.search_probe = search_probe if search_probe is not None else {
            "link_count": 3, "distinct_job_ids": 2, "sample_job_ids": ["111", "222"],
        }
        self.job_probe = job_probe or {"title": "Data Scientist", "easy_apply_present": True, "apply_controls": []}
        self.interactions = []

    def goto(self, url, **_):
        self.visited.append(url)
        self.url = self.redirects.get(url, url)

    def wait_for_load_state(self, *_a, **_k):
        pass

    def content(self):
        return "<html>fake</html>"

    def screenshot(self, **_):
        pass

    def evaluate(self, script, arg=None):
        if script is SEARCH_PROBE_JS:
            return self.search_probe
        if script is JOB_PROBE_JS:
            return self.job_probe
        raise AssertionError("unexpected script")

    # anything that would be an interaction must never be called
    def click(self, *a, **k):
        self.interactions.append("click")

    def fill(self, *a, **k):
        self.interactions.append("fill")

    def type(self, *a, **k):
        self.interactions.append("type")


def run(tmp_path, page, *, waits=None, sleeps=None, context=None):
    context = context or FakeContext()
    prompts = waits if waits is not None else []

    @contextmanager
    def factory(_settings):
        yield SimpleNamespace(page=page, context=context)

    settings = Settings(_env_file=None, inspection_output_dir=tmp_path / "out")
    report = run_linkedin_inspection(
        settings, query="data scientist", location="India",
        wait_for_manual_completion=lambda p: prompts.append(p),
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        browser_factory=factory,
    )
    return report, context, prompts


def test_already_logged_in_reads_search_and_first_job_without_any_prompt(tmp_path):
    page = FakePage()
    report, _, prompts = run(tmp_path, page)
    assert report["completed"] is True and prompts == []
    assert [s["step"] for s in report["steps"]] == ["login", "search", "job_page"]
    assert report["steps"][0]["status"] == "already_logged_in"
    assert page.visited[0] == FEED
    assert page.visited[1].startswith(sel.LINKEDIN_JOBS_SEARCH_URL) and "keywords=data+scientist" in page.visited[1]
    assert page.visited[2] == "https://www.linkedin.com/jobs/view/111/"
    assert len(page.visited) == 3  # tiny footprint: feed + one search + one job
    assert page.interactions == []  # never clicks, fills or types


def test_report_is_always_written_and_files_saved(tmp_path):
    report, _, _ = run(tmp_path, FakePage())
    out = tmp_path / "out" / report["output_dir"].split("\\")[-1].split("/")[-1]
    data = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert data["completed"] is True
    assert (out / "02_search_results.html").exists() and (out / "03_job_page.html").exists()


def test_login_wall_pauses_for_the_person_then_continues(tmp_path):
    class Page(FakePage):
        def goto(self, url, **k):
            super().goto(url, **k)
            if url == FEED and len(self.visited) == 1:  # first visit is bounced to the login wall
                self.url = "https://www.linkedin.com/login"

    page = Page()
    report, _, prompts = run(tmp_path, page)
    assert report["completed"] is True
    assert len(prompts) == 1 and "never types your credentials" in prompts[0]
    assert report["steps"][0]["status"] == "completed_by_user"
    assert page.interactions == []


def test_a_checkpoint_is_left_to_the_person_and_not_retried_if_still_present(tmp_path):
    page = FakePage(redirects={FEED: "https://www.linkedin.com/checkpoint/challenge/abc?x=secret"})
    report, _, prompts = run(tmp_path, page)
    assert report["completed"] is False and len(prompts) == 1
    assert report["error_type"] == "LinkedInInspectionError"
    assert "secret" not in json.dumps(report)  # query strings are never recorded
    assert page.visited == [FEED, FEED]  # one re-check after the pause, then it stops
    assert page.interactions == []


def test_no_job_links_skips_the_job_page(tmp_path):
    page = FakePage(search_probe={"link_count": 0, "distinct_job_ids": 0, "sample_job_ids": []})
    report, _, _ = run(tmp_path, page)
    assert report["completed"] is True and len(page.visited) == 2
    assert "skipped" in report["steps"][-1]


def test_it_paces_between_navigations(tmp_path):
    sleeps = []
    run(tmp_path, FakePage(), sleeps=sleeps)
    assert len(sleeps) == 2 and all(2.0 <= s <= 5.0 for s in sleeps)


def test_missing_interactive_terminal_is_a_clear_report_not_a_crash(tmp_path):
    page = FakePage(redirects={FEED: "https://www.linkedin.com/login"})

    @contextmanager
    def factory(_s):
        yield SimpleNamespace(page=page, context=FakeContext())

    def no_stdin(_prompt):
        raise EOFError

    report = run_linkedin_inspection(
        Settings(_env_file=None, inspection_output_dir=tmp_path / "out"),
        wait_for_manual_completion=no_stdin, sleep=lambda s: None, browser_factory=factory,
    )
    assert report["completed"] is False and report["error_type"] == "EOFError"
    assert "interactive terminal" in report["error"]


# --- the read-only guard ---------------------------------------------------------------------------------------


def test_guard_lets_everything_through_until_armed_then_blocks_mutations_only():
    guard = ReadOnlyGuard()
    ctx = FakeContext()
    guard.attach(ctx)

    def send(method, url="https://www.linkedin.com/voyager/api/x?token=abc"):
        r = FakeRoute(method, url)
        ctx.handler(r)
        return r.action

    assert send("POST") == "continue"  # the person's own login POST must pass
    guard.arm()
    assert send("GET") == "continue" and send("HEAD") == "continue" and send("OPTIONS") == "continue"
    assert send("POST") == "abort" and send("put") == "abort" and send("DELETE") == "abort"
    assert guard.blocked_total == 3
    assert guard.blocked[0] == {"method": "POST", "path": "/voyager/api/x"}  # path only, no query/token


def test_the_report_lists_blocked_requests_without_query_strings(tmp_path):
    ctx = FakeContext()
    page = FakePage()
    original_goto = page.goto

    def goto(url, **k):
        original_goto(url, **k)
        if ctx.handler and len(page.visited) == 2:  # while on the search page the site fires a tracking POST
            ctx.handler(FakeRoute("POST", "https://www.linkedin.com/li/track?session=SECRET"))

    page.goto = goto
    report, _, _ = run(tmp_path, page, context=ctx)
    assert report["blocked_mutating_requests"]["total"] == 1
    assert "SECRET" not in json.dumps(report)


def test_no_raw_css_selectors_leak_into_the_module():
    import inspect

    import naukri_agent.browser.linkedin_inspection as mod

    src = inspect.getsource(mod)
    for needle in ("a[href", "button,", "[role="):
        assert needle not in src.replace(SEARCH_PROBE_JS, "").replace(JOB_PROBE_JS, "")


@pytest.mark.parametrize("name", ["LINKEDIN_JOB_LINK", "LINKEDIN_JOB_TITLE", "LINKEDIN_APPLY_CONTROL_CANDIDATES"])
def test_linkedin_selectors_are_declared_unverified(name):
    from pathlib import Path

    text = Path(sel.__file__).read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith(name))
    assert "UNVERIFIED" in line
