"""Shared test setup."""

from __future__ import annotations

import subprocess

import pytest


@pytest.fixture(autouse=True)
def _never_touch_the_real_task_scheduler(monkeypatch):
    """A test must never ask the real Windows Task Scheduler anything, and above all never START the real phone listener.
    Tests that exercise orchestration/phone_control.py replace this with their own fake."""
    from naukri_agent.orchestration import phone_control

    def refuse(args):
        return subprocess.CompletedProcess(["schtasks", *args], returncode=1, stdout="", stderr="blocked in tests")

    monkeypatch.setattr(phone_control, "_run_schtasks", refuse)
