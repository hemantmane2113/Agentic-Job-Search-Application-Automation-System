"""Keep the PC awake while a long job runs, and let it sleep again afterwards."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import naukri_agent.cli.main as cli_main
from naukri_agent import power
from naukri_agent.config import Settings


class Recorder:
    def __init__(self, accept=True):
        self.calls, self.accept = [], accept

    def __call__(self, flags):
        self.calls.append(flags)
        return self.accept


def test_it_asks_for_the_system_to_stay_on_and_withdraws_the_request_afterwards(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(power, "_set_state", rec)
    with power.keep_awake("test") as held:
        assert held is True and rec.calls == [power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED]
    assert rec.calls[-1] == power.ES_CONTINUOUS  # the request is withdrawn: the normal sleep timer applies again


def test_it_never_asks_for_the_screen_to_stay_on(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(power, "_set_state", rec)
    with power.keep_awake():
        pass
    assert not any(flags & 0x00000002 for flags in rec.calls)  # ES_DISPLAY_REQUIRED is never set


def test_the_request_is_withdrawn_even_when_the_job_raises(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(power, "_set_state", rec)
    with pytest.raises(RuntimeError):
        with power.keep_awake():
            raise RuntimeError("the run failed")
    assert rec.calls[-1] == power.ES_CONTINUOUS


def test_when_windows_refuses_nothing_is_withdrawn_and_the_job_still_runs(monkeypatch):
    rec = Recorder(accept=False)
    monkeypatch.setattr(power, "_set_state", rec)
    with power.keep_awake() as held:
        assert held is False
    assert len(rec.calls) == 1  # only the request; there was nothing to release


def test_on_a_system_without_the_windows_call_it_is_a_quiet_no_op(monkeypatch):
    monkeypatch.setattr(power.os, "name", "posix")
    assert power._set_state(power.ES_CONTINUOUS) is False
    with power.keep_awake() as held:
        assert held is False


def test_a_missing_or_broken_windows_call_never_stops_a_run(monkeypatch):
    import ctypes

    class Broken:
        class kernel32:
            @staticmethod
            def SetThreadExecutionState(flags):
                raise OSError("no")

    monkeypatch.setattr(power.os, "name", "nt")
    monkeypatch.setattr(ctypes, "windll", Broken, raising=False)
    assert power._set_state(power.ES_CONTINUOUS) is False


# --- which commands keep the PC awake ------------------------------------------------------------------------------------


def test_the_long_running_commands_keep_the_pc_awake_and_the_always_on_listener_does_not():
    keep = cli_main._KEEP_AWAKE_COMMANDS
    assert {"run-daily", "research-jobs", "weekly-report", "profile-refresh", "telegram-apply"} <= keep
    assert "telegram-listen" not in keep and "scheduler" not in keep and "doctor" not in keep


def test_a_long_command_holds_the_request_for_exactly_as_long_as_it_runs(tmp_path, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(power, "_set_state", rec)
    c = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 't.db'}", weekly_report_dir=tmp_path / "w",
                 timezone="Asia/Kolkata", email_sender="file", email_output_dir=tmp_path / "e")
    monkeypatch.setattr(cli_main, "get_settings", lambda: c)
    res = CliRunner().invoke(cli_main.cli, ["weekly-report", "--no-email"])
    assert res.exit_code == 0, res.output
    assert rec.calls[0] == power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED and rec.calls[-1] == power.ES_CONTINUOUS


def test_a_quick_command_does_not_touch_the_power_state(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(power, "_set_state", rec)
    CliRunner().invoke(cli_main.cli, ["--help"])
    assert rec.calls == []
