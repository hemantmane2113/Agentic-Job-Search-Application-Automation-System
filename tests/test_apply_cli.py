"""Phase 14 `apply` CLI command: the AUTO_APPLY + DRY_RUN double gate,
and that run_apply_workflow is only ever invoked once both are set."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import naukri_agent.cli.main as cli_main
from naukri_agent.config import Settings
from naukri_agent.orchestration.apply_runner import ApplyRunResult


def _settings(**overrides) -> Settings:
    base = dict(_env_file=None, auto_apply=False, dry_run=True)
    base.update(overrides)
    return Settings(**base)


def _run(*args):
    return CliRunner().invoke(cli_main.cli, list(args))


@pytest.fixture
def no_call_guard(monkeypatch):
    """Fails the test if run_apply_workflow is ever actually invoked."""

    def _boom(*a, **k):
        raise AssertionError("run_apply_workflow must not be called when the gate is closed")

    monkeypatch.setattr("naukri_agent.orchestration.apply_runner.run_apply_workflow", _boom)


def test_default_settings_refuse_to_run(monkeypatch, no_call_guard):
    monkeypatch.setattr(cli_main, "get_settings", lambda: _settings())
    result = _run("apply", "123")
    assert result.exit_code != 0
    assert "AUTO_APPLY" in result.output and "DRY_RUN" in result.output


def test_auto_apply_true_but_dry_run_true_still_refuses(monkeypatch, no_call_guard):
    monkeypatch.setattr(cli_main, "get_settings", lambda: _settings(auto_apply=True, dry_run=True))
    result = _run("apply", "123")
    assert result.exit_code != 0


def test_auto_apply_false_but_dry_run_false_still_refuses(monkeypatch, no_call_guard):
    monkeypatch.setattr(cli_main, "get_settings", lambda: _settings(auto_apply=False, dry_run=False))
    result = _run("apply", "123")
    assert result.exit_code != 0


def test_both_gates_open_invokes_run_apply_workflow(monkeypatch):
    settings = _settings(auto_apply=True, dry_run=False)
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)

    canned = ApplyRunResult(
        job_id=1, title="Data Scientist", company="Acme", attempt_id="abc123",
        questions_asked=1, questions_skipped=0, submitted=True, application_id=5,
    )
    calls = []

    def _fake_run(settings_arg, job, *, isolated_profile=True):
        calls.append((settings_arg, job, isolated_profile))
        return canned

    monkeypatch.setattr(
        "naukri_agent.orchestration.apply_runner.run_apply_workflow", _fake_run
    )

    result = _run("apply", "123")
    assert result.exit_code == 0, result.output
    assert calls == [(settings, "123", True)]
    payload = json.loads(result.output)
    assert payload["submitted"] is True
    assert payload["application_id"] == 5


def test_reuse_session_flag_passes_isolated_profile_false(monkeypatch):
    settings = _settings(auto_apply=True, dry_run=False)
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)

    calls = []

    def _fake_run(settings_arg, job, *, isolated_profile=True):
        calls.append(isolated_profile)
        return ApplyRunResult(job_id=1, title="T", company="C", attempt_id="a", submitted=True)

    monkeypatch.setattr(
        "naukri_agent.orchestration.apply_runner.run_apply_workflow", _fake_run
    )

    _run("apply", "123", "--reuse-session")
    assert calls == [False]


def test_non_submitted_result_exits_nonzero(monkeypatch):
    settings = _settings(auto_apply=True, dry_run=False)
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        "naukri_agent.orchestration.apply_runner.run_apply_workflow",
        lambda *a, **k: ApplyRunResult(
            job_id=1, title="T", company="C", attempt_id="a", submitted=False,
            aborted_reason="user declined final confirmation",
        ),
    )
    result = _run("apply", "123")
    assert result.exit_code != 0
