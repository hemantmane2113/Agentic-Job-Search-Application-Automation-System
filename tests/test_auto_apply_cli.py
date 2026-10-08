"""auto-apply CLI gate and the per-job capture reset that multi-job sessions need."""

from click.testing import CliRunner

import naukri_agent.cli.main as cli_main
from naukri_agent.browser.apply_workflow import ApplyWorkflowSession
from naukri_agent.config import Settings

from .browser_fakes import FakePage


def test_auto_apply_command_refuses_and_exits_2_unless_every_switch_is_on(monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: Settings(_env_file=None))  # defaults: all off
    result = CliRunner().invoke(cli_main.cli, ["auto-apply"])
    assert result.exit_code == 2
    assert "AUTO_APPLY_UNATTENDED=true" in result.output and "blocked_reason" in result.output


def test_auto_apply_defaults_are_off_and_conservative():
    s = Settings(_env_file=None)
    assert s.auto_apply is False and s.dry_run is True and s.auto_apply_unattended is False
    assert s.auto_apply_daily_cap == 3 and s.auto_apply_decisions == ["ACCEPT"]


def test_capture_reset_forgets_the_previous_jobs_apply_init_response():
    """The capture keeps only the FIRST response it sees; without a reset a
    session handling several jobs would keep reading job #1's questions."""
    session = ApplyWorkflowSession(FakePage())
    session._capture.body = {"questionnaire": [{"id": "q1", "question": "old job's question"}]}
    session._capture.notes.append("note")
    session.reset_capture()
    assert session._capture.body is None and session._capture.notes == []
    assert session.list_questions() == []


def test_telegram_apply_refuses_without_a_bot_token_and_chat_id(monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: Settings(_env_file=None, auto_apply=True, dry_run=False))
    result = CliRunner().invoke(cli_main.cli, ["telegram-apply"])
    assert result.exit_code != 0 and "TELEGRAM_BOT_TOKEN" in result.output


def test_telegram_apply_with_telegram_set_but_gates_off_is_blocked_with_exit_2(monkeypatch):
    sent = []

    class _Quiet:
        def notify(self, text):
            sent.append(text)

    monkeypatch.setattr(
        cli_main, "get_settings",
        lambda: Settings(_env_file=None, telegram_bot_token="t", telegram_chat_id="1"),  # auto_apply False, dry_run True
    )
    monkeypatch.setattr(
        "naukri_agent.orchestration.telegram_interaction.build_telegram_interaction", lambda s, channel=None: _Quiet()
    )
    result = CliRunner().invoke(cli_main.cli, ["telegram-apply"])
    assert result.exit_code == 2 and "AUTO_APPLY=true" in result.output
    assert sent == []  # a blocked run sends nothing to the phone


def test_telegram_setup_needs_the_bot_token_first(monkeypatch):
    monkeypatch.setattr(cli_main, "get_settings", lambda: Settings(_env_file=None))
    result = CliRunner().invoke(cli_main.cli, ["telegram-setup"])
    assert result.exit_code != 0 and "TELEGRAM_BOT_TOKEN" in result.output
