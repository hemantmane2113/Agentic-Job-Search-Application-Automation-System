"""
The real Stage 1 validation: runs naukri-agent's actual inspection
tool against the real site. See tests/manual/README.md for
requirements. NEVER runs automatically.
"""

import pytest

from naukri_agent.browser.inspection import run_inspection
from naukri_agent.config import get_settings


@pytest.mark.manual
def test_stage1_inspection_completes_without_being_blocked():
    settings = get_settings()
    report = run_inspection(settings)
    assert report["completed"] is True, (
        f"Inspection stopped at {report.get('stopped_at')}: {report.get('error')}"
    )


@pytest.mark.manual
def test_stage1_inspection_captures_all_expected_steps():
    settings = get_settings()
    report = run_inspection(settings)
    step_names = {s["step"] for s in report["steps"]}
    assert {"login", "profile_resume", "search_jobs", "job_application_workflow"} <= step_names
