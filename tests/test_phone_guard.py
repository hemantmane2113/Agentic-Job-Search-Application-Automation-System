"""The phone guard: restart a dead listener within minutes, and say once a day that the PC is alive.

A PC that has frozen, been switched off or lost its internet cannot send an alert, so for someone away from it the only
signal is a daily message that stops arriving."""

from __future__ import annotations

import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner
from sqlalchemy.exc import OperationalError

import naukri_agent.cli.main as cli_main
from naukri_agent.config import Settings
from naukri_agent.database.base import session_scope
from naukri_agent.database.models import DailyRun, DailyRunStatus
from naukri_agent.orchestration import phone_guard as pg
from naukri_agent.orchestration.watchdog import TaskInfo

from .digest_fakes import in_memory_factory

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent


def cfg(tmp_path, **over):
    base = dict(phone_guard_state_file=tmp_path / "guard.json", telegram_bot_token="T", telegram_chat_id="1",
                telegram_remote_start=True)
    base.update(over)
    return Settings(_env_file=None, **base)


def at(h, m=0, day=22):
    return datetime.datetime(2026, 10, day, h, m, tzinfo=IST).astimezone(datetime.UTC)


STATUS_OK = {"run": "completed", "finished": "10:45", "new_jobs": 18, "read": 50, "reason": None, "recommendations": 1,
             "ready": 2, "remaining": 4, "cap": 4}


def go(tmp_path, when, *, phone="running", status=None, sent=None, **over):
    sent = sent if sent is not None else []
    result = pg.run_phone_guard(
        cfg(tmp_path, **over), now=when, ensure_fn=lambda s: phone, notify=sent.append,
        status_fn=lambda s, n: dict(status or STATUS_OK),
    )
    return result, sent


# --- when the alive message is due -----------------------------------------------------------------------------------------


def test_the_alive_message_is_due_from_ten_fifty_until_the_awake_window_ends(tmp_path):
    s = cfg(tmp_path)
    assert not pg.alive_due(s, at(10, 49).timestamp(), {})
    assert pg.alive_due(s, at(10, 50).timestamp(), {}) and pg.alive_due(s, at(22, 59).timestamp(), {})
    assert not pg.alive_due(s, at(23, 0).timestamp(), {})
    assert not pg.alive_due(s, at(12).timestamp(), {"alive_sent": True})
    assert not pg.alive_due(cfg(tmp_path, alive_message_enabled=False), at(12).timestamp(), {})


def test_the_time_is_checked_in_the_settings():
    with pytest.raises(ValueError):
        Settings(_env_file=None, alive_message_time="25:00")
    assert Settings(_env_file=None).alive_message_time == "10:50"


# --- what it says -----------------------------------------------------------------------------------------------------------------


def text(phone="running", **status):
    s = cfg(Path("."))
    return pg.build_alive_text(phone, {**STATUS_OK, **status}, s, at(10, 50).astimezone(IST))


def test_a_good_day_says_so_in_a_few_plain_lines():
    t = text()
    assert t.startswith("Daily check (Thu 22 Oct, 10:50): the PC is on and the app is alive.")
    assert "Phone control is running, so /apply works." in t
    assert "finished at 10:45 - 50 jobs read, 18 new. Digest emailed: 1 job(s) to apply yourself on company sites." in t
    assert "2 job(s) ready; you can approve up to 4 more today. Send /apply." in t
    assert "remind you at 19:00" in t


def test_problems_are_said_plainly():
    assert "NOT running" in text(phone="failed") and "Please check the PC" in text(phone="failed")
    assert "FAILED today (discovery: all queries failed)" in text(run="failed", reason="discovery: all queries failed")
    assert "still running" in text(run="running") and "still running" in text(run="busy")
    assert "has not run today" in text(run="not_run")
    assert "nothing new to apply for yourself" in text(recommendations=0)
    assert "no job is ready right now" in text(ready=0)
    assert "limit (4 per 24h) is used" in text(ready=3, remaining=0)


def test_a_dry_run_word_from_task_scheduler_is_understood():
    assert "Phone control is running" in text(phone="Running")


# --- the guard ------------------------------------------------------------------------------------------------------------------


def test_every_tick_checks_the_listener_but_the_alive_message_goes_once_a_day(tmp_path):
    calls = []
    sent = []
    for minute in (0, 10, 20):
        pg.run_phone_guard(cfg(tmp_path), now=at(11, minute), ensure_fn=lambda s: calls.append(1) or "running",
                           notify=sent.append, status_fn=lambda s, n: dict(STATUS_OK))
    assert len(calls) == 3 and len(sent) == 1 and sent[0].startswith("Daily check")


def test_nothing_is_sent_before_the_time_and_a_new_day_sends_again(tmp_path):
    _r, sent = go(tmp_path, at(10, 30))
    assert sent == []
    _r, sent = go(tmp_path, at(10, 50))
    assert len(sent) == 1
    _r, sent2 = go(tmp_path, at(10, 50, day=23))
    assert len(sent2) == 1


def test_a_listener_it_had_to_restart_is_not_a_noisy_message(tmp_path):
    # the listener itself sends "Phone control is on" when it starts, so the guard stays quiet about a restart
    result, sent = go(tmp_path, at(9, 0), phone="started")
    assert result.phone_control == "started" and sent == []


def test_a_listener_that_cannot_be_started_is_reported_once_a_day(tmp_path):
    result, sent = go(tmp_path, at(9, 0), phone="failed")
    assert result.down_alerted and len(sent) == 1 and "could not be started" in sent[0] and "Naukri Agent - Phone Control" in sent[0]
    result, sent2 = go(tmp_path, at(9, 10), phone="failed")
    assert not result.down_alerted and sent2 == []  # not every ten minutes
    result, sent3 = go(tmp_path, at(9, 10, day=23), phone="disabled")
    assert result.down_alerted and len(sent3) == 1


def test_a_failed_send_is_retried_at_the_next_tick(tmp_path):
    def broken(text):
        raise RuntimeError("telegram is down")

    result = pg.run_phone_guard(cfg(tmp_path), now=at(11), ensure_fn=lambda s: "running", notify=broken,
                                status_fn=lambda s, n: dict(STATUS_OK))
    assert not result.alive_sent and any("could not send" in n for n in result.notes)
    _r, sent = go(tmp_path, at(11, 10))
    assert len(sent) == 1  # it tried again, because the first one never went out


def test_a_dry_run_starts_nothing_sends_nothing_and_remembers_nothing(tmp_path):
    called = []
    result = pg.run_phone_guard(cfg(tmp_path), now=at(11), ensure_fn=lambda s: called.append(1) or "started",
                                notify=called.append, status_fn=lambda s, n: dict(STATUS_OK), dry_run=True)
    assert called == [] and result.preview and result.preview.startswith("Daily check")
    assert not (tmp_path / "guard.json").exists()


def test_without_telegram_it_says_so_and_does_not_crash(tmp_path):
    result = pg.run_phone_guard(cfg(tmp_path, telegram_bot_token=""), now=at(11), ensure_fn=lambda s: "running",
                                status_fn=lambda s, n: dict(STATUS_OK))
    assert not result.alive_sent and any("Telegram is not set up" in n for n in result.notes)


# --- reading today's facts --------------------------------------------------------------------------------------------------------


def run_row(status, started, finished=None, reason=None, new=18, read=50):
    return DailyRun(status=status, started_at=started, finished_at=finished, failure_reason=reason,
                    jobs_discovered=new, jobs_evaluated=read)


def facts(tmp_path, rows=(), task_state=None, now=None):
    factory = in_memory_factory()
    with session_scope(factory) as s:
        for r in rows:
            s.add(r)
    info = (lambda name: TaskInfo(state=task_state)) if task_state else (lambda name: None)
    return pg.gather_status(cfg(tmp_path), now or at(11), session_factory=factory, task_info_fn=info,
                            ready_fn=lambda s, n: (2, 3, 4))


def test_a_run_finished_today_is_reported_with_its_numbers(tmp_path):
    out = facts(tmp_path, [run_row(DailyRunStatus.COMPLETED, at(10, 0).replace(tzinfo=None), at(10, 45).replace(tzinfo=None))])
    assert (out["run"], out["finished"], out["new_jobs"], out["read"]) == ("completed", "10:45", 18, 50)
    assert (out["ready"], out["remaining"], out["cap"]) == (2, 3, 4)


def test_a_failed_run_is_reported_with_its_reason(tmp_path):
    out = facts(tmp_path, [run_row(DailyRunStatus.FAILED, at(10, 0).replace(tzinfo=None), at(10, 1).replace(tzinfo=None),
                                   reason="discovery failed: Error", new=0, read=0)])
    assert out["run"] == "failed" and out["reason"] == "discovery failed: Error"


def test_yesterdays_run_does_not_count_as_todays(tmp_path):
    out = facts(tmp_path, [run_row(DailyRunStatus.COMPLETED, at(10, 0, day=21).replace(tzinfo=None))], task_state="Ready")
    assert out["run"] == "not_run"


def test_no_record_yet_but_the_task_is_running_means_it_is_still_working(tmp_path):
    assert facts(tmp_path, task_state="Running")["run"] == "running"


def test_a_busy_database_means_the_run_is_still_working(tmp_path):
    def locked():
        raise OperationalError("x", {}, Exception("database is locked"))

    def factory():
        locked()

    out = pg.gather_status(cfg(tmp_path), at(11), session_factory=factory, task_info_fn=lambda n: None,
                           ready_fn=lambda s, n: (_ for _ in ()).throw(OperationalError("x", {}, Exception("locked"))))
    assert out["run"] == "busy" and out["ready"] is None  # reported as "still running", not as an error


# --- the command and the task ---------------------------------------------------------------------------------------------------------


def test_the_command_prints_what_it_did_and_always_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: cfg(tmp_path))
    res = CliRunner().invoke(cli_main.cli, ["phone-guard", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert '"phone_control"' in res.output and "dry run" in res.output


def test_the_guard_is_not_one_of_the_commands_that_revive_the_listener_themselves():
    assert "phone-guard" not in cli_main._REVIVE_PHONE_CONTROL_COMMANDS  # it does its own, and reports on it


def test_the_registration_script_makes_a_hidden_ten_minute_task_and_stops_the_network_condition_killing_the_listener():
    script = (ROOT / "scripts" / "register_phone_guard.ps1").read_text(encoding="utf-8")
    assert "Naukri Agent - Phone Guard" in script and "pythonw.exe" in script  # pythonw: no console window every 10 minutes
    assert "New-TimeSpan -Minutes 10" in script and "phone-guard" in script
    assert "RunOnlyIfNetworkAvailable = $false" in script and "Naukri Agent - Phone Control" in script
    assert "MultipleInstances IgnoreNew" in script
