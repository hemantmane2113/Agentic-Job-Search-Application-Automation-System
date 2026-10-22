"""The phone listener is revived whenever a scheduled command finds it dead.

Found 2026-10-22: Windows ended the listener's task when the PC slept (0x8007042B, "process terminated unexpectedly"), and
nothing started it again, so the PC was free to sleep and /apply went unheard for the whole day - and, it turned out, for
six days from 15 Oct. The task is only started at logon."""

from __future__ import annotations

import subprocess
import sys

import pytest
from click.testing import CliRunner

import naukri_agent.cli.main as cli_main
from naukri_agent.config import Settings
from naukri_agent.orchestration import phone_control as pc
from naukri_agent.orchestration import watchdog as wd

NAME = "Naukri Agent - Phone Control"


def cfg(**over):
    base = dict(telegram_remote_start=True, telegram_bot_token="T", telegram_chat_id="1")
    base.update(over)
    return Settings(_env_file=None, **base)


class FakeScheduler:
    """Stands in for schtasks. `statuses` is what successive /query calls report."""

    def __init__(self, statuses, run_ok=True):
        self.statuses, self.run_ok, self.calls = list(statuses), run_ok, []

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "/query":
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            if status is None:
                return subprocess.CompletedProcess(args, 1, "", "ERROR: The system cannot find the file specified.")
            return subprocess.CompletedProcess(args, 0, f'"\\{NAME}","N/A","{status}"\n', "")
        return subprocess.CompletedProcess(args, 0 if self.run_ok else 1, "", "")

    def ran(self):
        return [c for c in self.calls if c[0] == "/run"]


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(pc.sys, "platform", "win32")


def go(monkeypatch, fake, settings=None):
    monkeypatch.setattr(pc, "_run_schtasks", fake)
    return pc.ensure_phone_control_running(settings or cfg(), sleep=lambda s: None, verify_seconds=3)


# --- the decision ---------------------------------------------------------------------------------------------------


def test_a_running_listener_is_left_alone(windows, monkeypatch):
    fake = FakeScheduler(["Running"])
    assert go(monkeypatch, fake) == "running" and fake.ran() == []


def test_a_listener_that_windows_ended_is_started_again_and_confirmed(windows, monkeypatch):
    fake = FakeScheduler(["Ready", "Ready", "Running"])  # the check, then the task takes a moment to show as running
    assert go(monkeypatch, fake) == "started"
    assert fake.ran() == [["/run", "/tn", NAME]]


def test_a_start_that_is_refused_or_never_shows_as_running_is_reported_as_failed(windows, monkeypatch):
    assert go(monkeypatch, FakeScheduler(["Ready"], run_ok=False)) == "failed"
    assert go(monkeypatch, FakeScheduler(["Ready"])) == "failed"  # asked to run, but never reports Running


def test_a_task_a_person_disabled_is_not_forced_back_on(windows, monkeypatch):
    fake = FakeScheduler(["Disabled"])
    assert go(monkeypatch, fake) == "disabled" and fake.ran() == []


def test_a_missing_task_or_an_unreadable_scheduler_is_unavailable(windows, monkeypatch):
    assert go(monkeypatch, FakeScheduler([None])) == "unavailable"

    def broken(args):
        raise OSError("schtasks not found")

    assert go(monkeypatch, broken) == "unavailable"


def test_it_does_nothing_when_phone_control_or_auto_restart_is_off_or_not_windows(windows, monkeypatch):
    fake = FakeScheduler(["Ready"])
    assert go(monkeypatch, fake, cfg(telegram_remote_start=False)) == "skipped"
    assert go(monkeypatch, fake, cfg(phone_control_auto_restart=False)) == "skipped"
    assert go(monkeypatch, fake, cfg(telegram_bot_token="")) == "skipped"
    monkeypatch.setattr(pc.sys, "platform", "linux")
    assert go(monkeypatch, fake) == "skipped"
    assert fake.calls == []  # it did not even ask


def test_the_status_is_read_from_the_last_column(monkeypatch):
    monkeypatch.setattr(pc, "_run_schtasks", FakeScheduler(["Running"]))
    assert pc.phone_control_status(NAME) == "Running"


# --- the scheduled commands call it ---------------------------------------------------------------------------------------


def test_the_scheduled_commands_check_the_listener_and_the_others_do_not(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pc, "ensure_phone_control_running", lambda s: calls.append(1) or "running")
    c = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 't.db'}", weekly_report_dir=tmp_path / "w",
                 timezone="Asia/Kolkata", email_sender="file", email_output_dir=tmp_path / "e")
    monkeypatch.setattr(cli_main, "get_settings", lambda: c)
    res = CliRunner().invoke(cli_main.cli, ["weekly-report", "--no-email"])
    assert res.exit_code == 0, res.output
    assert calls == [1]
    CliRunner().invoke(cli_main.cli, ["--help"])
    CliRunner().invoke(cli_main.cli, ["doctor"])
    assert calls == [1]  # a quick command does not ask


def test_which_commands_revive_it():
    assert {"profile-refresh", "run-daily", "research-jobs", "weekly-report"} <= cli_main._REVIVE_PHONE_CONTROL_COMMANDS
    assert not {"telegram-listen", "telegram-apply", "doctor", "watchdog"} & cli_main._REVIVE_PHONE_CONTROL_COMMANDS


def test_a_failure_in_the_check_never_stops_a_scheduled_command(tmp_path, monkeypatch):
    def boom(s):
        raise RuntimeError("scheduler exploded")

    monkeypatch.setattr(pc, "ensure_phone_control_running", boom)
    c = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 't.db'}", weekly_report_dir=tmp_path / "w",
                 timezone="Asia/Kolkata", email_sender="file", email_output_dir=tmp_path / "e")
    monkeypatch.setattr(cli_main, "get_settings", lambda: c)
    assert CliRunner().invoke(cli_main.cli, ["weekly-report", "--no-email"]).exit_code == 0


# --- the watchdog tells you ---------------------------------------------------------------------------------------------------


def run_dog(tmp_path, phone, dry_run=False):
    sent = []
    s = Settings(_env_file=None, watchdog_state_file=tmp_path / "wd.json", database_url=f"sqlite:///{tmp_path / 'w.db'}",
                 profile_refresh_enabled=False, telegram_remote_start=True)
    result = wd.run_watchdog(
        s, now=__import__("datetime").datetime(2026, 10, 22, 5, 0, tzinfo=__import__("datetime").UTC),
        task_info_fn=lambda n: wd.TaskInfo(state="Ready"), notify=sent.append, dry_run=dry_run,
        phone_control_fn=lambda _s: phone, session_factory=None,
    )
    return result, sent


def test_the_watchdog_says_so_when_it_had_to_restart_the_listener(tmp_path):
    result, sent = run_dog(tmp_path, "started")
    assert [p.kind for p in result.problems if p.kind.startswith("phone")] == ["phone_control_restarted"]
    assert len(sent) == 1 and "had stopped" in sent[0] and "send it again" in sent[0]
    result2, sent2 = run_dog(tmp_path, "started")  # the same day: not repeated
    assert sent2 == [] and "phone_control_restarted" in result2.already_alerted


def test_the_watchdog_warns_when_the_listener_cannot_be_started(tmp_path):
    for word in ("failed", "disabled"):
        result, sent = run_dog(tmp_path, word)
        assert any(p.kind == "phone_control_down" for p in result.problems)
        assert any("could not be started" in m for m in sent) or result.already_alerted


def test_a_healthy_or_unknown_listener_is_not_an_alert(tmp_path):
    for word in ("running", "skipped"):
        result, sent = run_dog(tmp_path, word)
        assert not any(p.kind.startswith("phone") for p in result.problems)
    result, _ = run_dog(tmp_path, "unavailable")
    assert "phone control could not be checked" in result.notes


def test_a_dry_run_never_starts_anything(tmp_path):
    called = []
    s = Settings(_env_file=None, watchdog_state_file=tmp_path / "wd.json", database_url=f"sqlite:///{tmp_path / 'w.db'}",
                 profile_refresh_enabled=False)
    wd.run_watchdog(s, task_info_fn=lambda n: wd.TaskInfo(state="Ready"), dry_run=True,
                    phone_control_fn=lambda _s: called.append(1) or "started", session_factory=None)
    assert called == []


def test_the_awake_window_now_opens_early_enough_to_cover_a_nine_oclock_boot():
    assert Settings(_env_file=None).telegram_listener_awake_from <= "08:00"
