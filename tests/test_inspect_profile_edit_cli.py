"""`naukri-agent inspect-profile-edit` CLI surface — exercised through
CliRunner with run_profile_edit_inspection monkeypatched to a canned
report, mirroring `inspect`/`inspect-apply`'s own exit-code contract."""

from __future__ import annotations

from click.testing import CliRunner

import naukri_agent.cli.main as cli_main
from naukri_agent.config import Settings


def _settings() -> Settings:
    return Settings(_env_file=None)


def _run(*args):
    return CliRunner().invoke(cli_main.cli, list(args))


def test_completed_report_exits_zero(monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        "naukri_agent.browser.profile_inspection.run_profile_edit_inspection",
        lambda settings, isolated_profile=True, capture_after_manual_open=False: {"completed": True, "steps": []},
    )
    result = _run("inspect-profile-edit")
    assert result.exit_code == 0, result.output


def test_incomplete_report_exits_nonzero(monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        "naukri_agent.browser.profile_inspection.run_profile_edit_inspection",
        lambda settings, isolated_profile=True, capture_after_manual_open=False: {"completed": False, "steps": []},
    )
    result = _run("inspect-profile-edit")
    assert result.exit_code != 0


def test_reuse_session_flag_passes_isolated_profile_false(monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: _settings())
    calls = []

    def _fake(settings, isolated_profile=True, capture_after_manual_open=False):
        calls.append(isolated_profile)
        return {"completed": True, "steps": []}

    monkeypatch.setattr(
        "naukri_agent.browser.profile_inspection.run_profile_edit_inspection", _fake
    )
    _run("inspect-profile-edit", "--reuse-session")
    assert calls == [False]


def test_manual_open_capture_is_on_by_default_and_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: _settings())
    seen = []

    def _fake(settings, isolated_profile=True, capture_after_manual_open=False):
        seen.append(capture_after_manual_open)
        return {"completed": True, "steps": []}

    monkeypatch.setattr("naukri_agent.browser.profile_inspection.run_profile_edit_inspection", _fake)
    _run("inspect-profile-edit")
    _run("inspect-profile-edit", "--no-manual-open")
    assert seen == [True, False]
