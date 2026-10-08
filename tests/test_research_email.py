"""The research email for Part 1 jobs: careers page, how you'll apply, differences; sent once after the digest."""

from __future__ import annotations

import datetime
import json

import pytest
from click.testing import CliRunner
from sqlalchemy.exc import OperationalError

import naukri_agent.cli.main as cli_main
from naukri_agent.config import Settings
from naukri_agent.database.base import session_scope
from naukri_agent.database.models import DailyRunStatus, JobResearch
from naukri_agent.research_agent import runner as runner_mod
from naukri_agent.research_agent.ats import detect_apply_method
from naukri_agent.research_agent.models import AgentLLMError
from naukri_agent.research_agent.runner import compose_email, run_research, todays_digest_is_ready, wait_for_digest

from .digest_fakes import in_memory_factory, make_run
from .test_research_agent import CAREERS, ROLE, FakeSearch, ScriptedClient, call, page, submit, turn
from .test_research_runner import NOW, FakeReader, cfg, ok_script, seed

# --- recognising how applying will go ------------------------------------------------------------------------------------


def raw(mapping):
    def _fetch(url):
        if url not in mapping:
            raise OSError("unreachable")
        return url, mapping[url]

    return _fetch


def test_a_standard_system_is_recognised_and_described_as_easy():
    m = detect_apply_method([CAREERS], raw({CAREERS: '<a href="https://jobs.lever.co/acme/123">Apply</a>'}))
    assert (m.name, m.level) == ("Lever", "easy") and "without creating an account" in m.note


def test_workday_and_icims_are_described_as_needing_an_account():
    for html, name in (('<a href="https://acme.wd5.myworkdayjobs.com/x">Apply</a>', "Workday"), ('<a href="https://careers-acme.icims.com/jobs">x</a>', "iCIMS")):
        m = detect_apply_method([CAREERS], raw({CAREERS: html}))
        assert (m.name, m.level) == (name, "account") and "create an account" in m.note


def test_a_named_system_beats_an_upload_form_which_beats_email_which_beats_a_linkedin_link():
    both = '<form><input type="file"></form><a href="https://boards.greenhouse.io/acme">apply</a>'
    assert detect_apply_method([CAREERS], raw({CAREERS: both})).name == "Greenhouse"
    upload = detect_apply_method([CAREERS], raw({CAREERS: '<form><input type="file"></form> email your resume to hr@acme.example <a href="https://linkedin.com/jobs/acme">'}))
    assert upload.level == "own_form"
    email = detect_apply_method([CAREERS], raw({CAREERS: 'Please email your CV to careers@acme.example today. <a href="https://www.linkedin.com/jobs/acme">'}))
    assert email.level == "email" and "emailing your CV" in email.note
    board = detect_apply_method([CAREERS], raw({CAREERS: '<a href="https://www.linkedin.com/jobs/view/1">Apply on LinkedIn</a>'}))
    assert board.level == "board"


def test_a_page_with_nothing_recognisable_says_so_and_unreadable_pages_give_none():
    m = detect_apply_method([CAREERS], raw({CAREERS: "<p>Welcome to Acme</p>"}))
    assert m.level == "unknown" and "no standard application system" in m.note and m.line() == m.note
    assert detect_apply_method([CAREERS], raw({})) is None
    assert detect_apply_method([], raw({})) is None


def test_it_reads_at_most_three_distinct_pages_and_survives_one_failing():
    asked = []

    def fetch(url):
        asked.append(url)
        if url.endswith("/bad"):
            raise OSError("down")
        return url, '<a href="https://apply.workable.com/acme">x</a>'

    urls = ["https://a.example/bad", "https://a.example/one", "https://a.example/one", "https://a.example/two", "https://a.example/three", "https://a.example/four"]
    m = detect_apply_method(urls, fetch)
    assert m.name == "Workable" and len(asked) == 3 and asked.count("https://a.example/one") == 1


# --- the email -------------------------------------------------------------------------------------------------------------------------


def run(tmp_path, factory, script, *, email=None, raw_html="<p>plain</p>", **kw):
    client = ScriptedClient(script)
    client.model = "fake-model"
    result = run_research(
        cfg(tmp_path), session_factory=factory, chat_client=client, search=FakeSearch(), fetch=lambda u: page(u),
        reader=FakeReader(), email=email, now=NOW, fetch_raw_fn=lambda u: (u, raw_html), **kw,
    )
    return result


def test_one_email_lists_every_researched_job_with_the_careers_page_and_how_to_apply(tmp_path):
    f = in_memory_factory()
    seed(f, [("co1", "040926100101", "company_site"), ("co2", "040926100102", "company_site")])
    sent = []
    run(tmp_path, f, ok_script(2), email=lambda s, b: sent.append((s, b)),
        raw_html='<a href="https://boards.greenhouse.io/acme/jobs/1">Apply</a>')
    assert len(sent) == 1
    subject, body = sent[0]
    assert "Part 1 research: 2 job(s)" in subject
    assert "1. co1 role - co1 Co" in body and "2. co2 role - co2 Co" in body
    assert f"Careers page: {CAREERS}" in body and "Role listed there: yes" in body
    assert "How you'll apply: Greenhouse: a standard application form" in body
    assert f"Possible apply page: {ROLE} (high)" in body and "Naukri: https://www.naukri.com/" in body
    assert "researched. Check each careers page yourself" in body and "applied to nothing" in body


def test_the_how_to_apply_line_is_saved_with_the_report_and_comes_from_code(tmp_path):
    f = in_memory_factory()
    seed(f, [("co1", "040926100201", "company_site")])
    # the model tries to claim an apply method itself; code must overwrite it
    script = [turn(call("fetch_page", url=CAREERS)), turn(call("submit_report", company_summary="s", lists_this_role="yes",
              careers_url=CAREERS, apply_method="Totally Safe Form", apply_note="no login"))]
    run(tmp_path, f, script, raw_html='<a href="https://acme.wd5.myworkdayjobs.com/x">x</a>')
    with session_scope(f) as s:
        saved = json.loads(s.query(JobResearch).one().report_json)
    assert saved["apply_method"] == "Workday" and "create an account" in saved["apply_note"]


def test_failed_jobs_are_listed_in_the_email_and_a_run_with_nothing_sends_nothing(tmp_path):
    f = in_memory_factory()
    seed(f, [("co1", "040926100301", "company_site"), ("co2", "040926100302", "company_site")])
    sent = []
    run(tmp_path, f, [AgentLLMError("APIConnectionError")] + ok_script(1), email=lambda s, b: sent.append((s, b)))
    body = sent[0][1]
    assert "Part 1 research: 1 job(s)" in sent[0][0] and "Could not be researched: co1 role - co1 Co (llm_error: APIConnectionError)" in body
    empty = []
    f2 = in_memory_factory()
    run(tmp_path, f2, [], email=lambda s, b: empty.append(1))
    assert empty == []  # no jobs: no email


def test_a_dry_run_sends_no_email_and_an_email_failure_is_a_note_not_a_crash(tmp_path):
    f = in_memory_factory()
    seed(f, [("co1", "040926100401", "company_site")])
    sent = []
    run(tmp_path, f, ok_script(1), email=lambda s, b: sent.append(1), dry_run=True)
    assert sent == []
    f2 = in_memory_factory()
    seed(f2, [("co1", "040926100402", "company_site")])

    def boom(subject, body):
        raise RuntimeError("smtp down password=SECRET")

    result = run(tmp_path, f2, ok_script(1), email=boom)
    assert result.researched == 1 and any("could not send the research email" in n for n in result.notes)
    assert "SECRET" not in json.dumps(result.model_dump())


def test_compose_email_handles_nothing_researched():
    subject, body = compose_email([], ["a - b (llm_error: X)"], 1)
    assert "0 job(s)" in subject and "No job could be researched" in body


# --- waiting for the daily run -----------------------------------------------------------------------------------------------------------


def make_run_at(factory, started_utc, status):
    with session_scope(factory) as s:
        r = make_run(s, started_at=started_utc)
        r.status = status


def test_the_digest_is_ready_only_when_a_run_that_started_today_has_completed():
    c = Settings(_env_file=None)
    today_10am_ist = datetime.datetime(2026, 10, 9, 4, 30, tzinfo=datetime.UTC)
    now = datetime.datetime(2026, 10, 9, 8, 0, tzinfo=datetime.UTC)  # 13:30 IST

    f = in_memory_factory()
    make_run_at(f, today_10am_ist - datetime.timedelta(days=1), DailyRunStatus.COMPLETED)  # yesterday's
    assert todays_digest_is_ready(c, f, now) is False
    make_run_at(f, today_10am_ist, DailyRunStatus.FAILED)
    assert todays_digest_is_ready(c, f, now) is False
    make_run_at(f, today_10am_ist, DailyRunStatus.STARTED)
    assert todays_digest_is_ready(c, f, now) is False
    make_run_at(f, today_10am_ist, DailyRunStatus.COMPLETED)
    assert todays_digest_is_ready(c, f, now) is True


def test_the_day_boundary_is_india_time():
    c = Settings(_env_file=None)
    f = in_memory_factory()
    make_run_at(f, datetime.datetime(2026, 10, 8, 20, 0, tzinfo=datetime.UTC), DailyRunStatus.COMPLETED)  # 01:30 IST on the 9th
    assert todays_digest_is_ready(c, f, datetime.datetime(2026, 10, 9, 8, 0, tzinfo=datetime.UTC)) is True


def test_waiting_polls_until_the_run_appears():
    c = Settings(_env_file=None)
    f = in_memory_factory()
    clock = {"now": datetime.datetime(2026, 10, 9, 4, 35, tzinfo=datetime.UTC)}
    polls = []

    def sleep(seconds):
        polls.append(seconds)
        clock["now"] += datetime.timedelta(seconds=seconds)
        if len(polls) == 2:  # the run finishes while we wait
            make_run_at(f, datetime.datetime(2026, 10, 9, 4, 30, tzinfo=datetime.UTC), DailyRunStatus.COMPLETED)

    assert wait_for_digest(c, session_factory=f, poll_seconds=300, sleep=sleep, clock=lambda: clock["now"]) is True
    assert len(polls) == 2


def test_waiting_gives_up_after_the_time_limit():
    c = Settings(_env_file=None, research_wait_hours=1.0)
    f = in_memory_factory()
    clock = {"now": datetime.datetime(2026, 10, 9, 4, 35, tzinfo=datetime.UTC)}

    def sleep(seconds):
        clock["now"] += datetime.timedelta(seconds=seconds)

    assert wait_for_digest(c, session_factory=f, poll_seconds=900, sleep=sleep, clock=lambda: clock["now"]) is False
    assert clock["now"] - datetime.datetime(2026, 10, 9, 4, 35, tzinfo=datetime.UTC) >= datetime.timedelta(hours=1)


def test_a_busy_database_counts_as_not_ready_not_as_a_crash(monkeypatch):
    class Locked:
        def __call__(self):
            raise OperationalError("select", {}, Exception("database is locked"))

    assert todays_digest_is_ready(Settings(_env_file=None), Locked(), NOW) is False


# --- the command --------------------------------------------------------------------------------------------------------------------------


def test_the_command_emails_by_default_and_telegram_is_opt_in(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli_main, "get_settings", lambda: Settings(_env_file=None, telegram_bot_token="t", telegram_chat_id="1"))

    def fake_run(settings, **kw):
        seen.update(kw)
        return runner_mod.ResearchRunResult()

    monkeypatch.setattr(runner_mod, "run_research", fake_run)
    sent = []

    class FakeSender:
        def send(self, message):
            sent.append(message)

    monkeypatch.setattr("naukri_agent.notifications.email.build_email_sender", lambda s: FakeSender())
    monkeypatch.setattr("naukri_agent.orchestration.telegram_interaction.build_telegram_interaction", lambda s: type("T", (), {"notify": staticmethod(lambda t: None)})())
    CliRunner().invoke(cli_main.cli, ["research-jobs"])
    assert seen["email"] is not None and seen["notify"] is None
    seen["email"]("S", "B")
    assert sent[0].subject == "S" and sent[0].text_body == "B"
    CliRunner().invoke(cli_main.cli, ["research-jobs", "--telegram"])
    assert seen["notify"] is not None
    CliRunner().invoke(cli_main.cli, ["research-jobs", "--dry-run"])
    assert seen["email"] is None and seen["notify"] is None


def test_wait_for_digest_that_times_out_stops_before_researching(monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: Settings(_env_file=None))
    called = []
    monkeypatch.setattr(runner_mod, "wait_for_digest", lambda s: False)
    monkeypatch.setattr(runner_mod, "run_research", lambda s, **kw: called.append(1))
    r = CliRunner().invoke(cli_main.cli, ["research-jobs", "--wait-for-digest"])
    assert r.exit_code != 0 and "did not appear in time" in r.output and called == []


def test_waiting_survives_the_database_being_locked_when_it_first_tries_to_open_it(monkeypatch):
    c = Settings(_env_file=None)
    real = in_memory_factory()
    attempts = []

    def flaky_init(settings):
        attempts.append(1)
        if len(attempts) == 1:
            raise OperationalError("create table", {}, Exception("database is locked"))
        return real

    monkeypatch.setattr("naukri_agent.database.base.init_db", flaky_init)
    make_run_at(real, datetime.datetime(2026, 10, 9, 4, 30, tzinfo=datetime.UTC), DailyRunStatus.COMPLETED)
    clock = {"now": datetime.datetime(2026, 10, 9, 8, 0, tzinfo=datetime.UTC)}
    ready = wait_for_digest(c, poll_seconds=60, sleep=lambda s: clock.update(now=clock["now"] + datetime.timedelta(seconds=s)), clock=lambda: clock["now"])
    assert ready is True and len(attempts) == 2
