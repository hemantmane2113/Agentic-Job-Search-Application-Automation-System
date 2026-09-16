"""Stage B: scheduler/daemon.py — build_scheduler() is inspected directly;
run_scheduler()/BlockingScheduler.start() are never called in tests since
they block forever."""

from __future__ import annotations

import logging

import pytest
from apscheduler.triggers.cron import CronTrigger

from naukri_agent.config import Settings
from naukri_agent.scheduler.daemon import JOB_ID, _run_job_safely, build_scheduler


def _settings(**over) -> Settings:
    base = dict(_env_file=None, database_url="sqlite://")
    base.update(over)
    return Settings(**base)


def test_daily_run_time_rejects_bad_formats():
    for bad in ["10", "10:5:00", "25:00", "10:60", "not-a-time", ""]:
        with pytest.raises(Exception):
            _settings(daily_run_time=bad)


def test_daily_run_time_normalizes_valid_input():
    assert _settings(daily_run_time="9:5").daily_run_time == "09:05"
    assert _settings(daily_run_time="23:59").daily_run_time == "23:59"


def test_build_scheduler_configures_expected_daily_job():
    settings = _settings(daily_run_time="10:00", timezone="Asia/Kolkata")
    scheduler = build_scheduler(settings, job_fn=lambda settings: None)

    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.id == JOB_ID
    assert isinstance(job.trigger, CronTrigger)

    fields = {f.name: f for f in job.trigger.fields}
    assert str(fields["hour"]) == "10"
    assert str(fields["minute"]) == "0"


def test_build_scheduler_uses_configured_time_and_timezone():
    settings = _settings(daily_run_time="18:30", timezone="UTC")
    scheduler = build_scheduler(settings, job_fn=lambda settings: None)

    job = scheduler.get_jobs()[0]
    fields = {f.name: f for f in job.trigger.fields}
    assert str(fields["hour"]) == "18"
    assert str(fields["minute"]) == "30"


def test_run_job_safely_swallows_exceptions_and_logs_type_only(caplog):
    def failing_job(settings):
        raise RuntimeError("smtp password: hunter2")

    with caplog.at_level(logging.ERROR):
        _run_job_safely(_settings(), failing_job)  # must not raise

    assert any("RuntimeError" in r.message for r in caplog.records)
    assert not any("hunter2" in r.message for r in caplog.records)


def test_run_job_safely_logs_success(caplog):
    class _Result:
        run_id = 1
        status = "COMPLETED"
        recommendations = 2

    def ok_job(settings):
        return _Result()

    with caplog.at_level(logging.INFO):
        _run_job_safely(_settings(), ok_job)

    assert any("run_id=1" in r.message for r in caplog.records)
