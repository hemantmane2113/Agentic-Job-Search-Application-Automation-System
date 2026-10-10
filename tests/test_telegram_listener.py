"""Start applying from the phone: only YOUR chat is obeyed, only fresh commands count, nothing applies by itself,
and the listener stands aside while a telegram-apply run is going."""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy.exc import OperationalError

from naukri_agent.cli import main as cli_main
from naukri_agent.config import Settings
from naukri_agent.notifications.telegram import TelegramChannel, TelegramError
from naukri_agent.orchestration import telegram_listener as tl
from naukri_agent.orchestration.apply_lock import ApplyLockHeld, apply_lock_held, hold_apply_lock
from naukri_agent.orchestration.pipeline import _apply_ready_text
from naukri_agent.orchestration.telegram_listener import Listener

CHAT = "4242"
NOW = 1_800_000_000.0


def cfg(tmp_path, **over):
    base = dict(telegram_bot_token="T", telegram_chat_id=CHAT, telegram_remote_start=True,
                telegram_apply_lock_file=tmp_path / "lock", telegram_listener_max_command_age_seconds=600)
    base.update(over)
    return Settings(_env_file=None, **base)


class FakeChannel:
    def __init__(self, batches=()):
        self.batches = list(batches)
        self.sent: list[str] = []
        self.polled = 0

    def poll_commands(self, seconds):
        self.polled += 1
        return self.batches.pop(0) if self.batches else []

    def send(self, text, **_):
        self.sent.append(text)


def make(tmp_path, batches=(), *, summary=(3, 4, 4), spawn=(0, ""), lock=False, **over):
    ch = FakeChannel(batches)
    calls = {"spawn": 0, "env": None}

    def fake_spawn(settings):
        calls["spawn"] += 1
        return spawn

    def fake_summary(settings, now):
        if isinstance(summary, Exception):
            raise summary
        return summary

    sleeps = []
    lis = Listener(cfg(tmp_path, **over), ch, spawn=fake_spawn, summary=fake_summary, clock=lambda: NOW,
                   sleep=sleeps.append, lock_held=lambda _p: lock)
    return lis, ch, calls, sleeps


# --- /apply ----------------------------------------------------------------------------------------------------------


def test_apply_starts_the_run_and_says_what_will_happen(tmp_path):
    lis, ch, calls, _ = make(tmp_path, [[("/apply", NOW - 5)]])
    lis.step()
    assert calls["spawn"] == 1
    assert "3 job(s) will be offered" in ch.sent[0] and "Nothing is applied until you tap Yes" in ch.sent[0]


def test_the_number_offered_is_limited_by_the_daily_cap(tmp_path):
    lis, ch, calls, _ = make(tmp_path, [[("/apply", NOW)]], summary=(9, 2, 4))
    lis.step()
    assert "2 job(s) will be offered" in ch.sent[0]


@pytest.mark.parametrize("text", ["/apply", "/APPLY", "/apply@my_bot", " /apply please"])
def test_the_command_is_read_leniently(tmp_path, text):
    lis, _ch, calls, _ = make(tmp_path, [[(text, NOW)]])
    lis.step()
    assert calls["spawn"] == 1


def test_nothing_starts_when_nothing_is_ready_or_the_cap_is_used(tmp_path):
    lis, ch, calls, _ = make(tmp_path, [[("/apply", NOW)]], summary=(0, 4, 4))
    lis.step()
    assert calls["spawn"] == 0 and "No job is ready" in ch.sent[0]
    lis, ch, calls, _ = make(tmp_path, [[("/apply", NOW)]], summary=(5, 0, 4))
    lis.step()
    assert calls["spawn"] == 0 and "daily limit (4 per 24h)" in ch.sent[0]


def test_a_busy_database_is_explained_and_nothing_starts(tmp_path):
    lis, ch, calls, _ = make(tmp_path, [[("/apply", NOW)]], summary=OperationalError("x", {}, Exception("database is locked")))
    lis.step()
    assert calls["spawn"] == 0 and "daily run is probably still going" in ch.sent[0]


# --- old and foreign messages ---------------------------------------------------------------------------------------


def test_an_old_apply_is_not_acted_on_and_the_user_is_told(tmp_path):
    lis, ch, calls, _ = make(tmp_path, [[("/apply", NOW - 3600)]])
    lis.step()
    assert calls["spawn"] == 0 and "too old" in ch.sent[0]


def test_other_old_messages_are_ignored_quietly(tmp_path):
    lis, ch, calls, _ = make(tmp_path, [[("/status", NOW - 3600)]])
    lis.step()
    assert ch.sent == [] and calls["spawn"] == 0


def test_a_message_without_a_date_is_still_handled(tmp_path):
    lis, _ch, calls, _ = make(tmp_path, [[("/apply", None)]])
    lis.step()
    assert calls["spawn"] == 1


def test_plain_chatter_gets_no_reply_and_unknown_commands_get_the_help(tmp_path):
    lis, ch, calls, _ = make(tmp_path, [[("hello", NOW), ("/dance", NOW)]])
    lis.step()
    assert calls["spawn"] == 0 and len(ch.sent) == 1 and "/apply" in ch.sent[0]


def test_status_reports_without_starting_anything(tmp_path):
    lis, ch, calls, _ = make(tmp_path, [[("/status", NOW)]], summary=(4, 3, 4))
    lis.step()
    assert calls["spawn"] == 0 and "4 job(s) ready" in ch.sent[0] and "up to 3 more today" in ch.sent[0] and "/apply" in ch.sent[0]


def test_help_lists_the_commands(tmp_path):
    lis, ch, _calls, _ = make(tmp_path, [[("/help", NOW)]])
    lis.step()
    assert "/apply" in ch.sent[0] and "/status" in ch.sent[0]


# --- what happens after the run -----------------------------------------------------------------------------------------


def test_a_clean_run_adds_nothing_because_the_run_already_reported(tmp_path):
    lis, ch, _calls, _ = make(tmp_path, [[("/apply", NOW)]], spawn=(0, ""))
    lis.step()
    assert len(ch.sent) == 1  # only "Starting."


def test_a_blocked_run_says_why(tmp_path):
    out = '{\n  "blocked_reason": "paused: data\\\\PAUSE_AUTO_APPLY exists (delete it to resume)"\n}'
    lis, ch, _calls, _ = make(tmp_path, [[("/apply", NOW)]], spawn=(2, out))
    lis.step()
    assert ch.sent[-1].startswith("Could not start: paused")


def test_a_failed_run_relays_the_plain_error_line_only(tmp_path):
    out = "Traceback (most recent call last):\n  File secret.py\nError: The database is busy - the daily run is probably still going."
    lis, ch, _calls, _ = make(tmp_path, [[("/apply", NOW)]], spawn=(1, out))
    lis.step()
    assert "exit code 1" in ch.sent[-1] and "database is busy" in ch.sent[-1] and "Traceback" not in ch.sent[-1]


def test_a_timeout_is_reported(tmp_path):
    lis, ch, _calls, _ = make(tmp_path, [[("/apply", NOW)]], spawn=(-1, "timeout"))
    lis.step()
    assert "took too long" in ch.sent[-1]


# --- standing aside, errors, the process it starts -----------------------------------------------------------------


def test_while_an_apply_run_holds_the_lock_the_listener_does_not_read_telegram(tmp_path):
    lis, ch, calls, sleeps = make(tmp_path, [[("/apply", NOW)]], lock=True)
    lis.step()
    assert ch.polled == 0 and calls["spawn"] == 0 and sleeps == [5]


def test_a_dropped_connection_is_retried_with_a_growing_pause_and_never_crashes(tmp_path):
    class Flaky(FakeChannel):
        def poll_commands(self, seconds):
            self.polled += 1
            if self.polled <= 3:
                raise TelegramError("getUpdates failed: URLError/gaierror")
            return []

    ch = Flaky()
    sleeps = []
    lis = Listener(cfg(tmp_path), ch, spawn=lambda s: (0, ""), summary=lambda s, n: (0, 4, 4), clock=lambda: NOW,
                   sleep=sleeps.append, lock_held=lambda p: False)
    lis.run(should_stop=lambda: ch.polled >= 4)
    assert sleeps == [10, 30, 60] and ch.sent == [tl.HELP_TEXT]


def test_it_announces_itself_when_it_starts(tmp_path):
    lis, ch, _calls, _ = make(tmp_path)
    lis.run(should_stop=lambda: True)
    assert ch.sent == [tl.HELP_TEXT]


def test_the_started_process_gets_the_two_switches_and_this_process_does_not(tmp_path, monkeypatch):
    seen = {}

    class Done:
        returncode, stdout, stderr = 0, "ok", ""

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"], seen["timeout"] = cmd, kw["env"], kw["timeout"]
        return Done()

    monkeypatch.setattr(tl.subprocess, "run", fake_run)
    monkeypatch.setenv("AUTO_APPLY", "false")
    monkeypatch.setenv("DRY_RUN", "true")
    code, _out = tl.spawn_telegram_apply(cfg(tmp_path, telegram_listener_run_timeout_minutes=7))
    assert code == 0 and seen["cmd"][-1] == "telegram-apply" and seen["timeout"] == 420
    assert seen["env"]["AUTO_APPLY"] == "true" and seen["env"]["DRY_RUN"] == "false"
    assert os.environ["AUTO_APPLY"] == "false" and os.environ["DRY_RUN"] == "true"


def test_a_timed_out_process_is_reported_as_minus_one(tmp_path, monkeypatch):
    def boom(cmd, **kw):
        raise tl.subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(tl.subprocess, "run", boom)
    assert tl.spawn_telegram_apply(cfg(tmp_path))[0] == -1


def test_the_listener_never_imports_anything_that_can_apply():
    src = inspect.getsource(tl)
    for forbidden in ("auto_apply_runner", "apply_workflow", "apply_inspection", "prepare_application", "NaukriClient", "BrowserManager"):
        assert forbidden not in src.replace("`telegram-apply`", "")


# --- the channel's command reader -------------------------------------------------------------------------------------


def _channel(updates):
    calls = []

    def transport(method, params, timeout):
        calls.append(method)
        return {"ok": True, "result": updates if method == "getUpdates" else {}}

    return TelegramChannel("T", CHAT, transport=transport), calls


def test_poll_commands_returns_only_text_from_our_chat_with_its_time():
    ch, _ = _channel([
        {"update_id": 1, "message": {"chat": {"id": int(CHAT)}, "text": " /apply ", "date": 1700}},
        {"update_id": 2, "message": {"chat": {"id": 999}, "text": "/apply", "date": 1701}},
        {"update_id": 3, "callback_query": {"id": "x", "data": "y", "message": {"chat": {"id": int(CHAT)}}}},
        {"update_id": 4, "message": {"chat": {"id": int(CHAT)}, "date": 1702}},
    ])
    assert ch.poll_commands(0) == [("/apply", 1700)]


# --- the lock -----------------------------------------------------------------------------------------------------


def test_the_lock_is_held_during_a_run_and_removed_after(tmp_path):
    p = tmp_path / "lock"
    assert not apply_lock_held(p)
    with hold_apply_lock(p):
        assert apply_lock_held(p) and p.exists()
        with pytest.raises(ApplyLockHeld):
            with hold_apply_lock(p):
                pass
    assert not p.exists() and not apply_lock_held(p)


def test_a_lock_left_by_a_dead_process_is_ignored(tmp_path):
    p = tmp_path / "lock"
    p.write_text("99999999 2026-10-09T00:00:00+00:00\n")
    assert not apply_lock_held(p)
    with hold_apply_lock(p):
        assert apply_lock_held(p)


def test_the_lock_is_removed_even_when_the_run_fails(tmp_path):
    p = tmp_path / "lock"
    with pytest.raises(RuntimeError):
        with hold_apply_lock(p):
            raise RuntimeError("boom")
    assert not p.exists()


# --- the commands ------------------------------------------------------------------------------------------------------


def test_telegram_listen_refuses_to_run_unless_it_was_turned_on(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: cfg(tmp_path, telegram_remote_start=False))
    res = CliRunner().invoke(cli_main.cli, ["telegram-listen"])
    assert res.exit_code != 0 and "TELEGRAM_REMOTE_START=true" in res.output


def test_telegram_listen_needs_the_bot_to_be_set_up(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: cfg(tmp_path, telegram_bot_token="", telegram_chat_id=""))
    res = CliRunner().invoke(cli_main.cli, ["telegram-listen"])
    assert res.exit_code != 0 and "TELEGRAM_BOT_TOKEN" in res.output


def test_telegram_apply_refuses_to_start_while_another_run_holds_the_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: cfg(tmp_path, auto_apply=True, dry_run=False))
    with hold_apply_lock(tmp_path / "lock"):
        res = CliRunner().invoke(cli_main.cli, ["telegram-apply"])
    assert res.exit_code != 0 and "already in progress" in res.output


# --- the wording the phone and the email use --------------------------------------------------------------------------


JOBS = [{"title": "A", "company": "X", "score": 91.0, "resume_id": "r"}]


def test_the_ping_tells_you_to_send_apply_only_when_phone_start_is_on():
    assert "Send /apply here to start" in _apply_ready_text(JOBS, 3, 4, remote_start=True)
    assert "usual telegram-apply command" in _apply_ready_text(JOBS, 3, 4)
    assert "/apply" not in _apply_ready_text(JOBS, 0, 4, remote_start=True)  # cap used: nothing to start


def test_the_email_footer_mentions_the_phone_command_only_when_it_is_on():
    from naukri_agent.notifications.render import render_digest
    from naukri_agent.recommendations.models import RecommendationDigest

    def body(on: bool) -> str:
        import datetime

        d = RecommendationDigest(run_date=datetime.date(2026, 10, 9), generated_at=datetime.datetime(2026, 10, 9, 5, 0),
                                 candidate_email="c@example.com", limit=10, eligible_count=0, count=0, truncated=False,
                                 recommendations=[], run_id=1, two_part=True, native_waiting=2, telegram_slots=4)
        return render_digest(d, Settings(_env_file=None, telegram_remote_start=on)).text_body

    assert "Send /apply to your Telegram bot to start" in body(True)
    assert "Run: naukri-agent telegram-apply" in body(False) and "/apply to your Telegram bot" not in body(False)
