"""Replies must be heard at any time of day: the PC stays awake while the listener runs, a message that arrived while it
slept is explained rather than dropped silently, a 7 PM reminder lists the waiting jobs, and two commands never open the
shared browser profile at the same time."""

from __future__ import annotations

import datetime
import logging
import subprocess
import sys
import textwrap
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import OperationalError

from naukri_agent import power
from naukri_agent.browser import profile_lock
from naukri_agent.browser.browser_manager import BrowserManager
from naukri_agent.config import Settings
from naukri_agent.orchestration import apply_reminder as ar
from naukri_agent.orchestration.telegram_listener import Listener, _human_age
from naukri_agent.recommendations import apply_ready
from naukri_agent.safe_error import safe_error_text

from .test_telegram_listener import NOW, FakeChannel, cfg, make

IST = ZoneInfo("Asia/Kolkata")


def at(h: int, m: int = 0, day: int = 21) -> float:
    return datetime.datetime(2026, 10, day, h, m, tzinfo=IST).timestamp()


# --- the awake window ----------------------------------------------------------------------------------------------------


class Recorder:
    def __init__(self, accept=True):
        self.calls, self.accept = [], accept

    def __call__(self, flags):
        self.calls.append(flags)
        return self.accept


def t(h, m=0):
    return datetime.time(h, m)


def test_the_window_holds_the_pc_awake_inside_it_and_lets_go_outside(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(power, "_set_state", rec)
    w = power.AwakeWindow("09:40", "23:00")
    assert w.update(t(9, 39)) is False and rec.calls == []
    assert w.update(t(9, 40)) is True and rec.calls == [power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED]
    assert w.update(t(15)) is True and len(rec.calls) == 1  # asks Windows once, not every round
    assert w.update(t(22, 59)) is True
    assert w.update(t(23, 0)) is False and rec.calls[-1] == power.ES_CONTINUOUS


def test_a_window_that_wraps_midnight_works(monkeypatch):
    monkeypatch.setattr(power, "_set_state", Recorder())
    w = power.AwakeWindow("22:00", "02:00")
    assert w.inside(t(23)) and w.inside(t(1, 30)) and not w.inside(t(2)) and not w.inside(t(12))


def test_when_windows_refuses_it_is_not_asked_again_every_round(monkeypatch):
    rec = Recorder(accept=False)
    monkeypatch.setattr(power, "_set_state", rec)
    w = power.AwakeWindow("09:40", "23:00")
    for _ in range(5):
        assert w.update(t(12)) is False
    assert len(rec.calls) == 1


def test_the_screen_is_never_kept_on(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(power, "_set_state", rec)
    power.AwakeWindow("00:00", "23:59").update(t(12))
    assert not any(flags & 0x00000002 for flags in rec.calls)  # ES_DISPLAY_REQUIRED is never set


def test_the_listener_follows_the_window_by_the_clock_and_lets_go_when_it_stops(tmp_path, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(power, "_set_state", rec)
    now = {"t": at(9, 0)}
    ch = FakeChannel()
    lis = Listener(cfg(tmp_path, telegram_listener_keep_awake=True), ch, clock=lambda: now["t"], sleep=lambda s: None,
                   lock_held=lambda _p: False, browser_busy=lambda _p: None)
    lis._watch_the_clock()
    assert rec.calls == []  # 09:00 is before the window
    now["t"] = at(10, 0)
    lis._watch_the_clock()
    assert rec.calls == [power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED]
    stop = iter([False, True])
    lis.run(should_stop=lambda: next(stop))
    assert rec.calls[-1] == power.ES_CONTINUOUS  # leaving the loop hands the PC back to Windows


def test_the_window_can_be_switched_off(tmp_path, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(power, "_set_state", rec)
    lis, *_ = make(tmp_path)
    lis._clock = lambda: at(12)
    lis._watch_the_clock()
    assert rec.calls == []


def test_bad_times_in_the_settings_are_refused(tmp_path):
    for field in ("telegram_listener_awake_from", "telegram_listener_awake_until", "apply_reminder_time"):
        with pytest.raises(ValueError):
            Settings(_env_file=None, **{field: "25:00"})
    assert Settings(_env_file=None, apply_reminder_time="7:05").apply_reminder_time == "07:05"
    d = Settings(_env_file=None)
    assert (d.telegram_listener_awake_from, d.telegram_listener_awake_until, d.apply_reminder_time) == ("09:40", "23:00", "19:00")


# --- a message that arrived while the PC slept ----------------------------------------------------------------------------


def test_a_late_apply_is_logged_and_explained_once(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    lis, ch, calls, _ = make(tmp_path, [[("/apply", NOW - 7200), ("/apply", NOW - 7000), ("/status", NOW - 7100)]])
    lis.step()
    assert calls["spawn"] == 0
    assert len(ch.sent) == 1 and "2 hours ago" in ch.sent[0] and "asleep or off" in ch.sent[0]
    ignored = [r.getMessage() for r in caplog.records if "telegram message ignored" in r.getMessage()]
    assert len(ignored) == 3 and any("/status" in m for m in ignored) and all("limit 10 min" in m for m in ignored)


def test_ages_are_said_in_plain_units():
    assert _human_age(30) == "1 minute" and _human_age(600) == "10 minutes" and _human_age(3600) == "60 minutes"
    assert _human_age(7200) == "2 hours" and _human_age(3600 * 22) == "22 hours"


def test_a_long_silence_and_the_heartbeat_are_written_to_the_log(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    clock = {"t": NOW}
    lis, *_ = make(tmp_path)
    lis._clock = lambda: clock["t"]
    lis._watch_the_clock()
    clock["t"] += 60
    lis._watch_the_clock()
    assert not any("nothing was heard" in r.getMessage() for r in caplog.records)  # a normal pause
    clock["t"] += 5 * 3600  # the PC slept
    lis._watch_the_clock()
    gap = [r.getMessage() for r in caplog.records if "nothing was heard" in r.getMessage()]
    assert len(gap) == 1 and "5 hours" in gap[0]
    assert any("still listening" in r.getMessage() or "listening (PC kept awake" in r.getMessage() for r in caplog.records)


def test_apply_waits_when_another_command_has_the_browser(tmp_path):
    lis, ch, calls, _ = make(tmp_path, [[("/apply", NOW)]], listener_kw=dict(browser_busy=lambda _p: "run-daily (running since 10:00)"))
    lis.step()
    assert calls["spawn"] == 0
    assert "busy with run-daily (running since 10:00)" in ch.sent[0] and "again in a few minutes" in ch.sent[0]


def test_the_browser_check_can_be_switched_off(tmp_path):
    lis, ch, calls, _ = make(tmp_path, [[("/apply", NOW)]], browser_lock_enabled=False,
                             listener_kw=dict(browser_busy=lambda _p: "x"))
    lis.step()
    assert calls["spawn"] == 1


# --- the 7 PM reminder -------------------------------------------------------------------------------------------------------


def settings(tmp_path, **over):
    return cfg(tmp_path, **over)


def test_the_reminder_is_due_from_seven_until_the_awake_window_ends_and_only_once_a_day(tmp_path):
    s = settings(tmp_path)
    assert not ar.apply_reminder_due(s, at(18, 59))
    assert ar.apply_reminder_due(s, at(19, 0)) and ar.apply_reminder_due(s, at(22, 59))
    assert not ar.apply_reminder_due(s, at(23, 0))  # the PC is about to sleep; too late to be useful
    ar.mark_apply_reminder_handled(s, at(19, 5))
    assert not ar.apply_reminder_due(s, at(19, 6)) and not ar.apply_reminder_due(s, at(21, 0))
    assert ar.apply_reminder_due(s, at(19, 0, day=22))  # a new day
    assert not ar.apply_reminder_due(settings(tmp_path, apply_reminder_enabled=False), at(20))


def test_the_text_lists_jobs_with_the_date_each_leaves_the_list(tmp_path):
    jobs = [
        {"title": "Data Scientist", "company": "Acme", "score": 84.4, "expires": datetime.datetime(2026, 10, 26, 4, 0)},
        {"title": "ML Engineer", "company": "Beta", "score": 83.0, "expires": None},
    ]
    text = ar.reminder_text(jobs, 3, 4)
    assert text.startswith("Evening reminder: 2 jobs waiting")
    assert "1. Data Scientist - Acme  (score 84, on the list until 26 Oct)" in text
    assert "2. ML Engineer - Beta  (score 83, on the list)" in text
    assert "up to 3 more today" in text and "Send /apply to start" in text and "nothing is applied until you tap Yes" in text


def test_the_text_for_one_job_a_used_up_limit_and_a_long_list():
    one = ar.reminder_text([{"title": "T", "company": "C", "score": 90, "expires": None}], 0, 4)
    assert "1 job waiting" in one and "already used" in one and "Send /apply" not in one
    many = [{"title": f"T{i}", "company": "C", "score": 90, "expires": None} for i in range(11)]
    text = ar.reminder_text(many, 4, 4)
    assert "8. T7" in text and "9. T8" not in text and "... and 3 more" in text
    assert ar.reminder_text([], 4, 4) is None
    assert "naukri-agent telegram-apply" in ar.reminder_text(many, 4, 4, remote_start=False)


def reminder_listener(tmp_path, waiting, hour=19):
    s = settings(tmp_path)
    ch = FakeChannel()
    state = {"t": at(hour, 0)}
    lis = Listener(s, ch, clock=lambda: state["t"], sleep=lambda _s: None, lock_held=lambda _p: False,
                   browser_busy=lambda _p: None, waiting=waiting, followup=lambda *a, **k: "ok")
    return lis, ch, state, s


def test_at_seven_the_listener_sends_the_waiting_jobs_once(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    jobs = [{"title": "DS", "company": "Acme", "score": 84, "expires": datetime.datetime(2026, 10, 26)}]
    lis, ch, state, s = reminder_listener(tmp_path, lambda _s, _n: (jobs, 4, 4))
    lis.step()
    assert len(ch.sent) == 1 and "Evening reminder" in ch.sent[0] and "DS - Acme" in ch.sent[0]
    state["t"] += 120
    lis.step()
    assert len(ch.sent) == 1  # not again today
    assert any("apply reminder sent (1 job(s) waiting)" in r.getMessage() for r in caplog.records)


def test_nothing_waiting_means_no_message_and_no_nagging_later(tmp_path):
    lis, ch, state, s = reminder_listener(tmp_path, lambda _s, _n: ([], 4, 4))
    lis.step()
    state["t"] += 3600
    lis.step()
    assert ch.sent == []


def test_before_seven_nothing_is_sent(tmp_path):
    lis, ch, *_ = reminder_listener(tmp_path, lambda _s, _n: ([{"title": "T", "company": "C", "score": 90, "expires": None}], 4, 4), hour=18)
    lis.step()
    assert ch.sent == []


def test_a_busy_database_retries_in_ten_minutes(tmp_path):
    calls = {"n": 0}

    def waiting(_s, _n):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("x", {}, Exception("database is locked"))
        return [{"title": "T", "company": "C", "score": 90, "expires": None}], 4, 4

    lis, ch, state, s = reminder_listener(tmp_path, waiting)
    lis.step()
    assert ch.sent == [] and calls["n"] == 1
    state["t"] += 300
    lis.step()
    assert calls["n"] == 1  # too soon
    state["t"] += 400
    lis.step()
    assert calls["n"] == 2 and len(ch.sent) == 1


def test_a_failing_reminder_never_stops_phone_control(tmp_path):
    def waiting(_s, _n):
        raise RuntimeError("boom")

    lis, ch, state, s = reminder_listener(tmp_path, waiting)
    lis.step()  # must not raise
    assert ch.sent == []
    assert not ar.apply_reminder_due(s, state["t"])  # not retried all evening


def test_waiting_jobs_carry_the_date_they_leave_the_list(tmp_path, monkeypatch):
    seen = datetime.datetime(2026, 10, 19, 4, 30)

    class Job:
        last_seen_at = seen

    class Session:
        def get(self, _model, _id):
            return Job()

    monkeypatch.setattr(
        apply_ready, "select_candidates",
        lambda s, c, st, n: [{"job_id": 1, "title": "DS", "company": "Acme", "score": 84.0}],
    )
    out = apply_ready.waiting_jobs(Session(), 1, Settings(_env_file=None, auto_apply_max_job_age_days=7), datetime.datetime(2026, 10, 21))
    assert out == [{"title": "DS", "company": "Acme", "score": 84.0, "expires": datetime.datetime(2026, 10, 26, 4, 30)}]


# --- one browser at a time -----------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _forget_locks_held_by_this_process():
    yield
    profile_lock._held.clear()


def child(path, seconds=30):
    """A second process that takes the lock, says so, and holds it."""
    code = textwrap.dedent(f"""
        import sys, time
        from naukri_agent.browser import profile_lock
        profile_lock.acquire({str(path)!r}, 5)
        print("held", flush=True)
        time.sleep({seconds})
    """)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_a_second_process_waits_and_then_gives_up_with_a_clear_message(tmp_path):
    path = tmp_path / "browser.lock"
    p = child(path)
    try:
        busy = profile_lock.holder_if_busy(path)
        assert busy is not None and busy.startswith("python (running since ")  # names the other command and when it started
        with pytest.raises(profile_lock.BrowserBusy) as err:
            profile_lock.acquire(path, 0.2, sleep=lambda s: None, clock=iter([0.0, 0.0, 1.0, 2.0, 3.0]).__next__)
        assert "in use by" in str(err.value) and "gave up" in str(err.value)
    finally:
        p.kill()
        p.wait()


def test_when_the_other_process_dies_the_lock_frees_itself(tmp_path):
    path = tmp_path / "browser.lock"
    p = child(path)
    p.kill()
    p.wait()
    assert profile_lock.holder_if_busy(path) is None
    profile_lock.acquire(path, 1)  # no stale-lock cleanup needed
    profile_lock.release(path)


def test_the_lock_is_free_after_release_and_reentrant_inside_one_process(tmp_path):
    path = tmp_path / "browser.lock"
    assert profile_lock.holder_if_busy(path) is None  # no file yet
    profile_lock.acquire(path, 1)
    profile_lock.acquire(path, 1)  # a second browser in the same command must not wait for itself
    assert profile_lock.holder_if_busy(path) is None  # "other" processes only
    profile_lock.release(path)
    p = subprocess.run([sys.executable, "-c", f"from naukri_agent.browser import profile_lock as p; print(p.holder_if_busy({str(path)!r}) is not None)"],
                       capture_output=True, text=True)
    assert p.stdout.strip() == "True"  # still held after one release
    profile_lock.release(path)
    p = subprocess.run([sys.executable, "-c", f"from naukri_agent.browser import profile_lock as p; print(p.holder_if_busy({str(path)!r}) is not None)"],
                       capture_output=True, text=True)
    assert p.stdout.strip() == "False"


def test_a_waiting_process_gets_the_browser_as_soon_as_it_is_freed(tmp_path):
    path = tmp_path / "browser.lock"
    p = child(path, seconds=1.5)
    try:
        profile_lock.acquire(path, 30, sleep=lambda s: __import__("time").sleep(0.2))
    finally:
        p.kill()
        p.wait()
    profile_lock.release(path)


class FakePlaywright:
    def __init__(self, fail=False):
        self.fail = fail
        self.chromium = self

    def start(self):
        return self

    def launch_persistent_context(self, **_kw):
        if self.fail:
            raise RuntimeError("Target closed - password hunter22 was typed")
        ctx = self
        ctx.new_page = lambda: object()
        return ctx

    def close(self):
        pass

    def stop(self):
        pass


def manager(tmp_path, fail=False, monkeypatch=None, **over):
    s = Settings(_env_file=None, browser_profile_dir=tmp_path / "profile", browser_lock_file=tmp_path / "browser.lock",
                 naukri_password="hunter22", **over)
    import playwright.sync_api as pw

    monkeypatch.setattr(pw, "sync_playwright", lambda: FakePlaywright(fail))
    return BrowserManager(s), s


def test_a_launched_browser_holds_the_lock_until_it_closes(tmp_path, monkeypatch):
    m, s = manager(tmp_path, monkeypatch=monkeypatch)
    m.launch()
    p = subprocess.run([sys.executable, "-c", f"from naukri_agent.browser import profile_lock as p; print(p.holder_if_busy({str(s.browser_lock_file)!r}) is not None)"],
                       capture_output=True, text=True)
    assert p.stdout.strip() == "True"
    m.close()
    p = subprocess.run([sys.executable, "-c", f"from naukri_agent.browser import profile_lock as p; print(p.holder_if_busy({str(s.browser_lock_file)!r}) is not None)"],
                       capture_output=True, text=True)
    assert p.stdout.strip() == "False"
    m.close()  # idempotent


def test_a_failed_launch_lets_go_of_the_lock_and_logs_a_scrubbed_reason(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    m, s = manager(tmp_path, fail=True, monkeypatch=monkeypatch)
    with pytest.raises(RuntimeError):
        m.launch()
    assert profile_lock.holder_if_busy(s.browser_lock_file) is None and not profile_lock._held
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "browser launch failed: builtins.RuntimeError: Target closed" in logged
    assert "hunter22" not in logged and "***" in logged


def test_a_disposable_profile_or_a_disabled_lock_takes_no_lock(tmp_path, monkeypatch):
    m, s = manager(tmp_path, monkeypatch=monkeypatch, browser_lock_enabled=False)
    m.launch()
    assert not profile_lock._held
    m.close()
    m2, s2 = manager(tmp_path, monkeypatch=monkeypatch)
    m2._profile_dir_override = tmp_path / "disposable"
    m2.launch()
    assert not profile_lock._held
    m2.close()


# --- the log line for an error ---------------------------------------------------------------------------------------------


def test_the_error_text_is_one_short_scrubbed_line():
    s = Settings(_env_file=None, naukri_password="s3cretpass", naukri_email="me@example.com", telegram_bot_token="123:ABCDEF")
    exc = RuntimeError("could not fill me@example.com with s3cretpass\nsecond line with 123:ABCDEF")
    out = safe_error_text(exc, s)
    assert out == "builtins.RuntimeError: could not fill *** with ***"
    assert "\n" not in out and "second line" not in out


def test_the_error_text_is_cut_short_and_works_without_settings_or_a_message():
    assert safe_error_text(ValueError("x" * 500)).endswith("x" * 200) and len(safe_error_text(ValueError("x" * 500))) < 260
    assert safe_error_text(KeyError()) == "builtins.KeyError"
    assert safe_error_text(ValueError("tiny"), Settings(_env_file=None)) == "builtins.ValueError: tiny"
