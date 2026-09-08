"""
NaukriClient: the ONLY interface the rest of the system (CLI,
orchestration, future agents) should use to interact with Naukri. No
caller outside browser/ should ever see a CSS/XPath selector or a
Playwright type — that's the entire point of this facade.
"""

from __future__ import annotations

from typing import Any

from naukri_agent.browser import jobs as _jobs
from naukri_agent.browser import login as _login
from naukri_agent.browser import profile as _profile
from naukri_agent.browser.models import (
    ApplicationWorkflowInspection,
    JobListingSummary,
    LoginResult,
    ResumeState,
)
from naukri_agent.config import Settings


class NaukriClient:
    def __init__(self, page: Any, settings: Settings) -> None:
        self._page = page
        self._settings = settings

    def login(self) -> LoginResult:
        return _login.login(self._page, self._settings)

    def check_already_logged_in(self) -> LoginResult | None:
        """
        Non-destructive re-check of the current page — never
        navigates/fills/clicks. Used to avoid repeating login() when a
        human has already resolved a CAPTCHA/MFA challenge manually
        while paused (see browser/inspection.py).
        """
        return _login.check_already_logged_in(self._page)

    def get_profile_resume(self) -> ResumeState:
        return _profile.get_profile_resume(self._page)

    def search_jobs(self, query: str, location: str = "") -> list[JobListingSummary]:
        return _jobs.search_jobs(self._page, query, location)

    def get_job(self, url: str) -> ApplicationWorkflowInspection:
        return _jobs.inspect_application_workflow(self._page, url)

    def prepare_application(self, *args: Any, **kwargs: Any) -> Any:
        """
        Stage 2 (write operations). Deliberately not implemented until
        Stage 1's read-only inspection has been reviewed and approved
        — see the Phase 7 plan.
        """
        raise NotImplementedError(
            "prepare_application is Stage 2 (write operations) — not "
            "implemented until Stage 1 is reviewed and explicitly approved."
        )
