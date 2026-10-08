"""research-jobs: choosing the jobs, saving and sending the reports, and the separation guarantees."""

from __future__ import annotations

import datetime
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import naukri_agent.cli.main as cli_main
from naukri_agent.browser.jobs import read_company_page
from naukri_agent.config import Settings
from naukri_agent.database.base import session_scope
from naukri_agent.database.models import DailyRunStatus, JobRecommendation, JobResearch
from naukri_agent.database.repositories import add_job_research
from naukri_agent.matching.models import MatchDecision
from naukri_agent.research_agent import runner as runner_mod
from naukri_agent.research_agent.models import AgentLLMError
from naukri_agent.research_agent.runner import (
    ResearchConfigError,
    ResearchRunResult,
    format_report,
    run_research,
    select_jobs,
)
from naukri_agent.research_agent.models import ResearchReport

from .digest_fakes import add_job, in_memory_factory, make_candidate, make_run, settings
from .test_research_agent import CAREERS, ROLE, FakeSearch, ScriptedClient, call, page, submit, turn

NOW = datetime.datetime(2026, 10, 9, 5, 0, tzinfo=datetime.UTC)


def cfg(tmp_path, **over):
    base = dict(research_max_jobs=5, research_skip_days=7, brave_api_key="", groq_api_key="")
    base.update(over)
    return settings(tmp_path, **base)


def seed(factory, specs, *, completed=True):
    """specs: (slug, ext, apply_type). Returns {slug: job_id}. One recommendation per job, rank in order."""
    ids = {}
    with session_scope(factory) as s:
        cand = make_candidate(s)
        run = make_run(s)
        run.status = DailyRunStatus.COMPLETED if completed else DailyRunStatus.STARTED
        for rank, (slug, ext, apply_type) in enumerate(specs, start=1):
            job = add_job(s, slug=slug, ext=ext, title=f"{slug} role", company=f"{slug} Co")
            job.apply_type = apply_type
            s.add(JobRecommendation(
                candidate_id=cand.id, job_id=job.id, daily_run_id=run.id, rank=rank, score_at_email=90.0,
                decision_at_email=MatchDecision.ACCEPT,
            ))
            ids[slug] = job.id
    return ids


class FakeReader:
    def __init__(self):
        self.urls = []
        self.closed = False

    def read(self, url):
        self.urls.append(url)
        return {"title": "Co - Naukri", "rating": "3.4", "reviews": "10 Reviews", "text": "About the company", "links": []}

    def close(self):
        self.closed = True


def ok_script(n=1):
    out = []
    for _ in range(n):
        out += [turn(call("get_company_page")), turn(call("fetch_page", url=CAREERS)), turn(submit())]
    return out


def go(tmp_path, factory, script, *, notify=None, search="default", reader=None, **kw):
    client = ScriptedClient(script)
    client.model = "fake-model"
    rd = reader or FakeReader()
    result = run_research(
        cfg(tmp_path), session_factory=factory, chat_client=client,
        search=FakeSearch() if search == "default" else search,
        fetch=lambda url: page(url), reader=rd, notify=notify, now=NOW, **kw,
    )
    return result, rd, client


def saved(factory):
    with session_scope(factory) as s:
        return [(r.job_id, r.status, r.model, r.error, r.steps, r.prompt_tokens) for r in s.query(JobResearch).order_by(JobResearch.id)]


# --- choosing jobs -----------------------------------------------------------------------------------------------------


def test_only_company_site_jobs_of_the_latest_completed_digest_are_chosen_in_rank_order(tmp_path):
    f = in_memory_factory()
    ids = seed(f, [("native1", "040926000101", "native"), ("co1", "040926000102", "company_site"),
                   ("co2", "040926000103", "company_site"), ("unk", "040926000104", None)])
    with session_scope(f) as s:
        picked = select_jobs(s, cfg(tmp_path), NOW, job_id=None, limit=5)
    assert [j["job_id"] for j in picked] == [ids["co1"], ids["co2"]]
    with session_scope(f) as s:
        assert len(select_jobs(s, cfg(tmp_path), NOW, job_id=None, limit=1)) == 1


def test_no_completed_digest_means_nothing_to_research(tmp_path):
    f = in_memory_factory()
    seed(f, [("co1", "040926000201", "company_site")], completed=False)
    with session_scope(f) as s:
        assert select_jobs(s, cfg(tmp_path), NOW, job_id=None, limit=5) == []


def test_recently_researched_jobs_are_skipped_but_failures_and_old_work_are_retried(tmp_path):
    f = in_memory_factory()
    ids = seed(f, [("a", "040926000301", "company_site"), ("b", "040926000302", "company_site"), ("c", "040926000303", "company_site")])
    with session_scope(f) as s:
        add_job_research(s, job_id=ids["a"], status="ok", model="m", created_at=NOW - datetime.timedelta(days=2))
        add_job_research(s, job_id=ids["b"], status="failed", model="m", created_at=NOW - datetime.timedelta(days=1))
        add_job_research(s, job_id=ids["c"], status="ok", model="m", created_at=NOW - datetime.timedelta(days=30))
        picked = select_jobs(s, cfg(tmp_path), NOW, job_id=None, limit=5)
    assert {j["job_id"] for j in picked} == {ids["b"], ids["c"]}


def test_an_explicit_job_id_is_always_honoured(tmp_path):
    f = in_memory_factory()
    ids = seed(f, [("a", "040926000401", "company_site")])
    with session_scope(f) as s:
        add_job_research(s, job_id=ids["a"], status="ok", model="m", created_at=NOW)
        assert [j["job_id"] for j in select_jobs(s, cfg(tmp_path), NOW, job_id=ids["a"], limit=5)] == [ids["a"]]
        assert select_jobs(s, cfg(tmp_path), NOW, job_id=99999, limit=5) == []


# --- running -----------------------------------------------------------------------------------------------------------------


def test_a_run_saves_each_report_and_sends_it_to_telegram(tmp_path):
    f = in_memory_factory()
    ids = seed(f, [("co1", "040926000501", "company_site"), ("co2", "040926000502", "company_site")])
    sent = []
    result, reader, client = go(tmp_path, f, ok_script(2), notify=sent.append)
    assert (result.considered, result.researched, result.failed) == (2, 2, 0)
    assert [r[:3] for r in saved(f)] == [(ids["co1"], "ok", "fake-model"), (ids["co2"], "ok", "fake-model")]
    assert saved(f)[0][4:] == (3, 300)  # 3 steps, 3 x 100 prompt tokens
    assert len(sent) == 2 and "Research: co1 role - co1 Co" in sent[0] and f"Careers page: {CAREERS}" in sent[0]
    assert ROLE in sent[0] and "Lists this role: yes" in sent[0] and "Naukri: https://www.naukri.com/" in sent[0]
    assert len(reader.urls) == 2 and all(u.startswith("https://www.naukri.com/") for u in reader.urls)  # the job's own page only
    with session_scope(f) as s:
        report = json.loads(s.query(JobResearch).first().report_json)
    assert report["sources"] == [CAREERS]


def test_a_dry_run_prints_but_saves_and_sends_nothing(tmp_path):
    f = in_memory_factory()
    seed(f, [("co1", "040926000601", "company_site")])
    sent = []
    result, _, _ = go(tmp_path, f, ok_script(1), notify=sent.append, dry_run=True)
    assert result.researched == 1 and result.outcomes[0].message and saved(f) == [] and sent == []


def test_a_failed_job_is_recorded_without_a_message_and_the_next_one_still_runs(tmp_path):
    f = in_memory_factory()
    ids = seed(f, [("co1", "040926000701", "company_site"), ("co2", "040926000702", "company_site")])
    sent = []
    script = [AgentLLMError("APIConnectionError")] + ok_script(1)
    result, _, _ = go(tmp_path, f, script, notify=sent.append)
    assert (result.researched, result.failed) == (1, 1)
    assert saved(f)[0][1:4] == ("failed", "fake-model", "llm_error: APIConnectionError") and saved(f)[0][0] == ids["co1"]
    assert saved(f)[1][:2] == (ids["co2"], "ok") and len(sent) == 1
    assert result.outcomes[0].message is None


def test_a_telegram_failure_never_loses_the_saved_report(tmp_path):
    f = in_memory_factory()
    seed(f, [("co1", "040926000801", "company_site")])

    def boom(_text):
        raise RuntimeError("telegram down token=SECRET")

    result, _, _ = go(tmp_path, f, ok_script(1), notify=boom)
    assert saved(f)[0][1] == "ok" and result.researched == 1
    assert any("could not send" in n for n in result.notes) and "SECRET" not in json.dumps(result.model_dump())


def test_missing_search_key_is_noted_and_the_agent_is_told_search_is_unavailable(tmp_path):
    f = in_memory_factory()
    seed(f, [("co1", "040926000901", "company_site")])
    script = [turn(call("web_search", query="x")), turn(submit(careers_url=None, apply_candidates=[]))]
    result, _, client = go(tmp_path, f, script, search=None)
    assert any("no search key is set" in n for n in result.notes)
    tool_msg = [m for m in client.requests[-1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "not configured" in tool_msg


def test_a_missing_model_key_is_a_clear_error_before_anything_happens(tmp_path):
    with pytest.raises(ResearchConfigError, match="GROQ_API_KEY"):
        run_research(cfg(tmp_path), session_factory=in_memory_factory())


def test_nothing_to_research_is_a_note_not_an_error(tmp_path):
    f = in_memory_factory()
    result, _, client = go(tmp_path, f, [])
    assert result.considered == 0 and any("no company-site jobs" in n for n in result.notes) and client.requests == []


def test_a_reader_passed_in_is_not_closed_by_the_run(tmp_path):
    f = in_memory_factory()
    seed(f, [("co1", "040926001001", "company_site")])
    _, reader, _ = go(tmp_path, f, ok_script(1))
    assert reader.closed is False


def test_the_report_message_lists_flags_notes_and_stays_short():
    rep = ResearchReport(company_summary="A firm.", red_flags=["asks for a fee"], notes=["no web page was read"], sources=[])
    text = format_report({"title": "DS", "company": "Acme", "url": "https://www.naukri.com/j"}, rep)
    assert "Careers page: not found" in text and "Flags: asks for a fee" in text and "Note: no web page was read" in text
    assert len(text) < 3500


# --- the Naukri company page reader ---------------------------------------------------------------------------------


class FakePage:
    def __init__(self, company_href):
        self.company_href = company_href
        self.visited = []

    def goto(self, url, **_):
        self.visited.append(url)

    def wait_for_load_state(self, *_a, **_k):
        pass

    def wait_for_timeout(self, _ms):
        pass

    def evaluate(self, script, arg=None):
        return self.company_href if arg is not None else {"title": "Acme - Naukri", "rating": "3.2", "text": "About"}


def test_the_company_page_is_read_from_the_job_pages_own_link_only():
    p = FakePage("https://www.naukri.com/acme-jobs-careers-1")
    data = read_company_page(p, "https://www.naukri.com/job-listings-x")
    assert data["rating"] == "3.2" and p.visited == ["https://www.naukri.com/job-listings-x", "https://www.naukri.com/acme-jobs-careers-1"]


@pytest.mark.parametrize("href", [None, "https://evil.example/acme-jobs-careers-1", "https://naukri.com.evil.example/x", "http://127.0.0.1/"])
def test_a_company_link_that_is_not_on_naukri_is_never_visited(href):
    p = FakePage(href)
    with pytest.raises(ValueError, match="no Naukri company page link"):
        read_company_page(p, "https://www.naukri.com/job-listings-x")
    assert p.visited == ["https://www.naukri.com/job-listings-x"]


# --- the command ----------------------------------------------------------------------------------------------------------


def test_the_command_reports_counts_and_exits_nonzero_on_failures(monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: Settings(_env_file=None))
    results = iter([
        ResearchRunResult(considered=1, researched=1, outcomes=[runner_mod.ResearchOutcome(job_id=1, title="t", company="c", status="ok", message="REPORT TEXT")]),
        ResearchRunResult(considered=1, failed=1, outcomes=[runner_mod.ResearchOutcome(job_id=2, title="t2", company="c", status="failed", error="llm_error: X")]),
    ])
    monkeypatch.setattr(runner_mod, "run_research", lambda s, **kw: next(results))
    ok = CliRunner().invoke(cli_main.cli, ["research-jobs", "--dry-run"])
    assert ok.exit_code == 0 and "REPORT TEXT" in ok.output and "Researched 1 of 1" in ok.output
    bad = CliRunner().invoke(cli_main.cli, ["research-jobs"])
    assert bad.exit_code == 1 and "failed - llm_error: X" in bad.output


def test_a_missing_key_is_a_friendly_message(monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: Settings(_env_file=None))

    def boom(s, **kw):
        raise ResearchConfigError("GROQ_API_KEY is not set in .env")

    monkeypatch.setattr(runner_mod, "run_research", boom)
    r = CliRunner().invoke(cli_main.cli, ["research-jobs"])
    assert r.exit_code != 0 and "GROQ_API_KEY is not set" in r.output and "Traceback" not in r.output


# --- separation guarantees --------------------------------------------------------------------------------------------


def test_the_daily_run_discovery_and_scheduler_can_never_reach_the_agent():
    from naukri_agent.orchestration import discovery, pipeline
    from naukri_agent.scheduler import daemon

    for mod in (pipeline, discovery, daemon):
        assert "research_agent" not in inspect.getsource(mod)


def test_the_agent_package_has_no_path_to_applying_or_to_your_personal_data():
    pkg = Path(inspect.getsourcefile(runner_mod)).parent
    forbidden = ("auto_apply", "apply_workflow", "click_apply", "submit_application", "apply_runner",
                 "candidate_profile", "master_resume", "load_candidate_profile", "naukri_password", "smtp_")
    for f in pkg.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        for word in forbidden:
            assert word not in src, f"{f.name} mentions {word}"
