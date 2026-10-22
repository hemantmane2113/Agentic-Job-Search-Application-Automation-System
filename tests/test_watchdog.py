"""Morning watchdog: speaks only about real problems, once each per day, and never crashes."""

from __future__ import annotations

import datetime
import json
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import naukri_agent.cli.main as cli_main
from naukri_agent.database.base import session_scope
from naukri_agent.database.models import DailyRunStatus
from naukri_agent.database.repositories import add_resume_refresh
from naukri_agent.orchestration import watchdog as wd
from naukri_agent.orchestration.watchdog import TaskInfo, evaluate, read_task_info, run_watchdog

from .digest_fakes import in_memory_factory, make_run, settings

UTC = datetime.UTC


def ist(h, m=0, day=9):
    """A moment given in India time on 9 Oct 2026, as an aware UTC datetime."""
    return (datetime.datetime(2026, 10, day, h, m) - datetime.timedelta(hours=5, minutes=30)).replace(tzinfo=UTC)


def cfg(tmp_path, **over):
    base = dict(
        profile_refresh_enabled=True,
        profile_refresh_pause_file=tmp_path / "PAUSE_REFRESH",
        watchdog_state_file=tmp_path / "wd_state.json",
    )
    base.update(over)
    return settings(tmp_path, **base)


def ready_today(h=10, m=0):
    return TaskInfo(state="Ready", last_run=ist(h, m), last_result=0)


def kinds(problems):
    return [p.kind for p in problems]


GOOD = dict(refreshes_today=[("data_scientist", "uploaded")], runs_today=[("COMPLETED", None)])


# --- the decision logic ---------------------------------------------------------------------------------------------


def test_a_normal_day_has_no_problems(tmp_path):
    problems, notes = evaluate(cfg(tmp_path), now=ist(15, 5), task_info=ready_today(), **GOOD)
    assert problems == [] and notes == []


def test_nothing_is_reported_before_the_deadlines(tmp_path):
    problems, _ = evaluate(
        cfg(tmp_path), now=ist(9, 50),
        task_info=TaskInfo(state="Ready", last_run=ist(10, 0, day=8), last_result=0),
        refreshes_today=[], runs_today=[],
    )
    assert problems == []  # 09:50 is too early to call anything missing


def test_a_missing_refresh_is_reported_after_the_start_deadline(tmp_path):
    problems, _ = evaluate(cfg(tmp_path), now=ist(10, 45), task_info=ready_today(), refreshes_today=[], runs_today=[])
    assert kinds(problems) == ["refresh_missing"]


def test_the_refresh_is_not_checked_when_the_feature_is_off_or_paused(tmp_path):
    off = evaluate(cfg(tmp_path, profile_refresh_enabled=False), now=ist(10, 45), task_info=ready_today(),
                   refreshes_today=[], runs_today=[])[0]
    assert off == []
    c = cfg(tmp_path)
    c.profile_refresh_pause_file.write_text("x")
    assert evaluate(c, now=ist(10, 45), task_info=ready_today(), refreshes_today=[], runs_today=[])[0] == []


@pytest.mark.parametrize("outcome, words", [("needs_human", "CAPTCHA"), ("unconfirmed", "check"), ("failed", "failed")])
def test_a_refresh_that_ended_badly_says_why(tmp_path, outcome, words):
    problems, _ = evaluate(cfg(tmp_path), now=ist(10, 45), task_info=ready_today(),
                           refreshes_today=[("data_scientist", outcome)], runs_today=[])
    assert kinds(problems) == [f"refresh_{outcome}"] and words in problems[0].text


def test_a_later_successful_refresh_clears_an_earlier_failure(tmp_path):
    problems, _ = evaluate(cfg(tmp_path), now=ist(10, 45), task_info=ready_today(),
                           refreshes_today=[("a", "needs_human"), ("a", "uploaded")], runs_today=[])
    assert problems == []


def test_a_run_that_has_not_started_is_reported_with_the_task_state(tmp_path):
    info = TaskInfo(state="Ready", last_run=ist(10, 0, day=8), last_result=0)
    problems, _ = evaluate(cfg(tmp_path), now=ist(10, 45), task_info=info, **{**GOOD, "runs_today": []})
    assert kinds(problems) == ["run_not_started"]
    assert "last run 08 Oct 10:00" in problems[0].text and "Ready" in problems[0].text


def test_a_run_that_never_ran_is_reported(tmp_path):
    info = TaskInfo(state="Ready", last_run=None, last_result=267011)
    problems, _ = evaluate(cfg(tmp_path), now=ist(10, 45), task_info=info, **{**GOOD, "runs_today": []})
    assert "last run never" in problems[0].text and "0x41303" in problems[0].text


def test_a_run_in_progress_counts_as_started_and_is_fine_at_midmorning(tmp_path):
    info = TaskInfo(state="Running", last_run=ist(10, 0), last_result=267009)
    assert evaluate(cfg(tmp_path), now=ist(10, 45), task_info=info, **{**GOOD, "runs_today": []})[0] == []


def test_a_failed_run_is_reported_with_its_reason(tmp_path):
    problems, _ = evaluate(cfg(tmp_path), now=ist(15, 5), task_info=ready_today(),
                           refreshes_today=GOOD["refreshes_today"], runs_today=[("FAILED", "discovery: all queries failed")])
    assert kinds(problems) == ["run_failed"] and "discovery: all queries failed" in problems[0].text


def test_a_run_that_stopped_without_a_digest_is_reported(tmp_path):
    info = TaskInfo(state="Ready", last_run=ist(10, 0), last_result=0x41306)
    problems, _ = evaluate(cfg(tmp_path), now=ist(15, 5), task_info=info,
                           refreshes_today=GOOD["refreshes_today"], runs_today=[])
    assert kinds(problems) == ["run_no_digest"] and "No email was sent" in problems[0].text


def test_a_run_still_going_after_five_hours_is_reported_but_three_hours_is_normal(tmp_path):
    long = TaskInfo(state="Running", last_run=ist(10, 0), last_result=267009)
    assert kinds(evaluate(cfg(tmp_path), now=ist(15, 5), task_info=long,
                          refreshes_today=GOOD["refreshes_today"], runs_today=[])[0]) == ["run_long"]
    ok = evaluate(cfg(tmp_path), now=ist(15, 5), task_info=TaskInfo(state="Running", last_run=ist(12, 30), last_result=267009),
                  refreshes_today=GOOD["refreshes_today"], runs_today=[])[0]
    assert ok == []


def test_unreadable_task_scheduler_is_a_note_not_a_crash(tmp_path):
    problems, notes = evaluate(cfg(tmp_path), now=ist(15, 5), task_info=None, **GOOD)
    assert problems == [] and any("Task Scheduler could not be read" in n for n in notes)


def test_a_busy_database_is_a_note_not_a_false_alarm(tmp_path):
    problems, notes = evaluate(cfg(tmp_path), now=ist(15, 5), task_info=ready_today(),
                               refreshes_today=[], runs_today=[], db_readable=False)
    assert problems == [] and len(notes) == 2


# --- running it: once-a-day memory, safety, delivery -----------------------------------------------------------------------


def seeded(tmp_path, *, refresh=None, run=None):
    factory = in_memory_factory()
    with session_scope(factory) as s:
        if refresh:
            add_resume_refresh(s, resume_id="data_scientist", outcome=refresh, attempted_at=ist(9, 45))
        if run:
            r = make_run(s, started_at=ist(10, 0))
            r.status = DailyRunStatus(run)
            r.failure_reason = "boom" if run == "FAILED" else None
    return factory


def watch(c, factory, now, info, sent, **kw):
    return run_watchdog(c, now=now, task_info_fn=lambda _n: info, session_factory=factory, notify=sent.append, **kw)


def test_a_healthy_day_is_silent(tmp_path):
    sent = []
    r = watch(cfg(tmp_path), seeded(tmp_path, refresh="uploaded", run="COMPLETED"), ist(15, 5), ready_today(), sent)
    assert r.problems == [] and sent == []


def test_a_problem_is_sent_once_and_not_repeated_on_the_next_check(tmp_path):
    sent, c = [], cfg(tmp_path)
    factory = seeded(tmp_path, refresh="needs_human", run="COMPLETED")
    first = watch(c, factory, ist(10, 45), ready_today(), sent)
    assert first.alerted == ["refresh_needs_human"] and len(sent) == 1 and sent[0].startswith("Naukri watchdog:")
    again = watch(c, factory, ist(15, 5), ready_today(), sent)
    assert "refresh_needs_human" in again.already_alerted and again.alerted == [] and len(sent) == 1


def test_a_new_problem_later_the_same_day_is_still_sent(tmp_path):
    sent, c = [], cfg(tmp_path)
    watch(c, seeded(tmp_path, refresh="needs_human", run=None), ist(10, 45), ready_today(), sent)
    r = watch(c, seeded(tmp_path, refresh="needs_human", run="FAILED"), ist(15, 5), ready_today(), sent)
    assert r.alerted == ["run_failed"] and len(sent) == 2


def test_the_memory_resets_for_a_new_day(tmp_path):
    sent, c = [], cfg(tmp_path)
    factory = seeded(tmp_path, refresh="needs_human")
    watch(c, factory, ist(10, 45), ready_today(), sent)
    # next day: same condition (no refresh recorded for that day) is a fresh problem again
    r = watch(c, factory, ist(10, 45, day=10), TaskInfo(state="Ready", last_run=ist(10, 0, day=10), last_result=0), sent)
    assert "refresh_missing" in r.alerted and len(sent) == 2
    state = json.loads(c.watchdog_state_file.read_text())
    assert list(state) == ["2026-10-10"]  # only today is kept


def test_dry_run_sends_and_records_nothing(tmp_path):
    sent, c = [], cfg(tmp_path)
    r = watch(c, seeded(tmp_path, refresh="failed"), ist(10, 45), ready_today(), sent, dry_run=True)
    assert kinds(r.problems) == ["refresh_failed"] and sent == [] and not c.watchdog_state_file.exists()


def test_without_telegram_the_problem_is_still_returned_with_a_note(tmp_path):
    c = cfg(tmp_path)
    r = run_watchdog(c, now=ist(10, 45), task_info_fn=lambda _n: ready_today(),
                     session_factory=seeded(tmp_path, refresh="failed"))
    assert kinds(r.problems) == ["refresh_failed"] and any("Telegram is not set up" in n for n in r.notes)


def test_a_failed_send_is_not_remembered_so_the_next_check_tries_again(tmp_path):
    c = cfg(tmp_path)
    factory = seeded(tmp_path, refresh="failed", run="COMPLETED")

    def boom(_t):
        raise RuntimeError("network down token=SECRET")

    r = run_watchdog(c, now=ist(10, 45), task_info_fn=lambda _n: ready_today(), session_factory=factory, notify=boom)
    assert r.alerted == [] and any("could not send" in n for n in r.notes) and "SECRET" not in json.dumps(r.model_dump(mode="json"))
    sent = []
    again = watch(c, factory, ist(15, 5), ready_today(), sent)
    assert again.alerted == ["refresh_failed"] and len(sent) == 1


def test_the_all_good_message_is_optional_and_sent_once(tmp_path):
    sent, c = [], cfg(tmp_path, watchdog_send_ok=True)
    factory = seeded(tmp_path, refresh="uploaded", run="COMPLETED")
    assert watch(c, factory, ist(10, 45), ready_today(), sent).ok_message_sent is False  # digest not due yet
    assert watch(c, factory, ist(15, 5), ready_today(), sent).ok_message_sent is True
    assert watch(c, factory, ist(15, 30), ready_today(), sent).ok_message_sent is False
    assert len(sent) == 1 and "all good" in sent[0]


# --- reading Task Scheduler, and the command ------------------------------------------------------------------------------


def test_task_info_is_parsed_from_powershell_output(monkeypatch):
    payload = json.dumps({"State": "Running", "LastRun": "2026-10-09T04:30:30.0000000Z", "LastResult": 267009})
    monkeypatch.setattr(wd.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=payload + "\n"))
    info = read_task_info("Naukri Agent - Daily Jobs")
    assert info.state == "Running" and info.last_result == 267009
    assert info.last_run == datetime.datetime(2026, 10, 9, 4, 30, 30, tzinfo=UTC)


def test_a_task_that_never_ran_has_no_last_run(monkeypatch):
    payload = json.dumps({"State": "Ready", "LastRun": None, "LastResult": 267011})
    monkeypatch.setattr(wd.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=payload))
    assert read_task_info("x").last_run is None


def test_an_unreadable_task_gives_none_instead_of_raising(monkeypatch):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "powershell")

    monkeypatch.setattr(wd.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", boom)
    assert read_task_info("nope") is None


def test_the_command_exits_nonzero_when_there_is_a_problem(monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings(__import__("pathlib").Path("."), ))
    results = iter([wd.WatchdogResult(), wd.WatchdogResult(problems=[wd.Problem(kind="k", text="t")])])
    monkeypatch.setattr("naukri_agent.orchestration.watchdog.run_watchdog", lambda s, dry_run=False: next(results))
    runner = CliRunner()
    assert runner.invoke(cli_main.cli, ["watchdog"]).exit_code == 0
    assert runner.invoke(cli_main.cli, ["watchdog"]).exit_code == 1
