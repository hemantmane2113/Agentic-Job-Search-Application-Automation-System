"""CLI startup behavior: main()'s config-error handling and the
`scheduler` command's wiring to scheduler/daemon.py. Neither invokes a
real BlockingScheduler.start(), which blocks forever."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import naukri_agent.cli.main as cli_main


def test_main_reports_config_errors_cleanly_instead_of_a_traceback(monkeypatch, capsys):
    def _broken_settings():
        raise ValueError("daily_run_time must be 'HH:MM' (24-hour), got '25:00'")

    monkeypatch.setattr(cli_main, "get_settings", _broken_settings)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "configuration error" in captured.err
    assert "25:00" in captured.err
    assert "naukri-agent doctor" in captured.err


def test_main_runs_cli_when_settings_load_succeeds(monkeypatch):
    calls = []
    monkeypatch.setattr(cli_main, "get_settings", lambda: "fake-settings")
    monkeypatch.setattr(cli_main, "setup_logging", lambda settings: calls.append(settings))
    monkeypatch.setattr(cli_main, "cli", lambda: calls.append("cli-ran"))

    cli_main.main()

    assert calls == ["fake-settings", "cli-ran"]


def test_scheduler_command_delegates_to_run_scheduler(monkeypatch):
    calls = []
    monkeypatch.setattr(cli_main, "get_settings", lambda: "fake-settings")

    import naukri_agent.scheduler.daemon as daemon

    monkeypatch.setattr(daemon, "run_scheduler", lambda settings: calls.append(settings))

    result = CliRunner().invoke(cli_main.cli, ["scheduler"])

    assert result.exit_code == 0
    assert calls == ["fake-settings"]
