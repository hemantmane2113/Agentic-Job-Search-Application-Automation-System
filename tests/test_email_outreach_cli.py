"""Phase 15 `email-outreach` CLI command: the AUTO_EMAIL_OUTREACH +
DRY_RUN double gate, and that run_email_outreach_workflow is only ever
invoked once both are set."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import naukri_agent.cli.main as cli_main
from naukri_agent.config import Settings
from naukri_agent.orchestration.email_outreach_runner import EmailOutreachResult


def _settings(**overrides) -> Settings:
    base = dict(_env_file=None, auto_email_outreach=False, dry_run=True)
    base.update(overrides)
    return Settings(**base)


def _run(*args):
    return CliRunner().invoke(cli_main.cli, list(args))


@pytest.fixture
def no_call_guard(monkeypatch):
    """Fails the test if run_email_outreach_workflow is ever actually invoked."""

    def _boom(*a, **k):
        raise AssertionError("run_email_outreach_workflow must not be called when the gate is closed")

    monkeypatch.setattr(
        "naukri_agent.orchestration.email_outreach_runner.run_email_outreach_workflow", _boom
    )


def test_default_settings_refuse_to_run(monkeypatch, no_call_guard):
    monkeypatch.setattr(cli_main, "get_settings", lambda: _settings())
    result = _run("email-outreach", "123")
    assert result.exit_code != 0
    assert "AUTO_EMAIL_OUTREACH" in result.output and "DRY_RUN" in result.output


def test_auto_email_outreach_true_but_dry_run_true_still_refuses(monkeypatch, no_call_guard):
    monkeypatch.setattr(
        cli_main, "get_settings", lambda: _settings(auto_email_outreach=True, dry_run=True)
    )
    result = _run("email-outreach", "123")
    assert result.exit_code != 0


def test_auto_email_outreach_false_but_dry_run_false_still_refuses(monkeypatch, no_call_guard):
    monkeypatch.setattr(
        cli_main, "get_settings", lambda: _settings(auto_email_outreach=False, dry_run=False)
    )
    result = _run("email-outreach", "123")
    assert result.exit_code != 0


def test_both_gates_open_invokes_run_email_outreach_workflow(monkeypatch):
    settings = _settings(auto_email_outreach=True, dry_run=False)
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)

    canned = EmailOutreachResult(
        job_id=1, title="Data Scientist", company="Acme", attempt_id="abc123",
        sent=True, application_id=5,
    )
    calls = []

    def _fake_run(settings_arg, job):
        calls.append((settings_arg, job))
        return canned

    monkeypatch.setattr(
        "naukri_agent.orchestration.email_outreach_runner.run_email_outreach_workflow", _fake_run
    )

    result = _run("email-outreach", "123")
    assert result.exit_code == 0, result.output
    assert calls == [(settings, "123")]
    payload = json.loads(result.output)
    assert payload["sent"] is True
    assert payload["application_id"] == 5


def test_non_sent_result_exits_nonzero(monkeypatch):
    settings = _settings(auto_email_outreach=True, dry_run=False)
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        "naukri_agent.orchestration.email_outreach_runner.run_email_outreach_workflow",
        lambda *a, **k: EmailOutreachResult(
            job_id=1, title="T", company="C", attempt_id="a", sent=False,
            aborted_reason="user declined final confirmation",
        ),
    )
    result = _run("email-outreach", "123")
    assert result.exit_code != 0
