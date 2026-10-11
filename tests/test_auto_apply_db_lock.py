"""A locked database (daily run still going) gets a plain-English message, not a traceback."""

import pytest
from click.testing import CliRunner
from sqlalchemy.exc import OperationalError

import naukri_agent.cli.main as cli_main
from naukri_agent.config import Settings


def _locked(*_a, **_k):
    raise OperationalError("CREATE TABLE x", {}, Exception("database is locked"))


@pytest.mark.parametrize("command", ["auto-apply", "telegram-apply"])
def test_a_locked_database_is_explained_and_nothing_is_applied(monkeypatch, command):
    monkeypatch.setattr(
        cli_main, "get_settings",
        lambda: Settings(_env_file=None, auto_apply=True, dry_run=False, auto_apply_unattended=True,
                         telegram_bot_token="t", telegram_chat_id="1"),
    )
    monkeypatch.setattr("naukri_agent.orchestration.auto_apply_runner.run_auto_apply", _locked)
    monkeypatch.setattr(
        "naukri_agent.orchestration.telegram_interaction.build_telegram_interaction",
        lambda s, channel=None: object(),
    )
    result = CliRunner().invoke(cli_main.cli, [command])
    assert result.exit_code != 0
    assert "database is busy" in result.output and "Traceback" not in result.output
    assert "nothing was applied" in result.output.lower()


def test_other_database_errors_are_not_swallowed():
    def other():
        raise OperationalError("SELECT 1", {}, Exception("disk I/O error"))

    with pytest.raises(OperationalError):
        cli_main._explain_busy_database(other)
