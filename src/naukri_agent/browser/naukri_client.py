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
from naukri_agent.browser.client_interface import JobBoardClient
from naukri_agent.browser.models import (
    ApplicationWorkflowInspection,
    ApplyQuestionPrompt,
    ApplySubmissionResult,
    ApplyUiInspection,
    JobDetail,
    JobListingSummary,
    LoginResult,
    ResumeState,
)
from naukri_agent.config import Settings


_OPEN_JOB_ATTEMPTS = 3
_OPEN_JOB_RETRY_WAIT_MS = 3000


class NaukriClient(JobBoardClient):
    def __init__(self, page: Any, settings: Settings) -> None:
        super().__init__(page, settings)
        self._apply_session: Any = None

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

    def fetch_job_detail(self, url: str) -> JobDetail:
        """Read-only: open a job's public listing page and extract its
        title/company/location/experience/salary/posted/description. Never
        clicks/fills/submits; never touches the apply workflow."""
        return _jobs.fetch_job_detail(self._page, url)

    def get_job(self, url: str) -> ApplicationWorkflowInspection:
        return _jobs.inspect_application_workflow(self._page, url)

    def extract_application_ui(self) -> ApplyUiInspection:
        """
        Stage 1.5: READ the dynamically-rendered post-Apply UI on the
        page as it is right now. Never clicks/fills/navigates. See
        browser/apply_inspection.py for the full safety model — this is
        inspection only, not an application step.
        """
        # Imported lazily: apply_inspection imports NaukriClient, so a
        # module-level import here would be circular.
        from naukri_agent.browser import apply_inspection as _apply

        return _apply.extract_application_ui(self._page)

    def prepare_application(self, *args: Any, **kwargs: Any) -> Any:
        """
        Historical Stage 2 sentinel, predating the Phase 14 apply agent
        below. Deliberately left as a permanent NotImplementedError —
        not part of JobBoardClient's interface, Naukri-specific legacy
        naming only.
        """
        raise NotImplementedError(
            "prepare_application is Stage 2 (write operations) — not "
            "implemented until Stage 1 is reviewed and explicitly approved."
        )

    # --- Phase 14: apply-write surface ---
    # Delegates to browser/apply_workflow.py. Imported lazily (same
    # pattern as extract_application_ui() above) since apply_workflow
    # imports from apply_inspection, which imports NaukriClient — a
    # module-level import here would be circular.

    def _get_apply_session(self) -> Any:
        if self._apply_session is None:
            from naukri_agent.browser.apply_workflow import ApplyWorkflowSession

            self._apply_session = ApplyWorkflowSession(self._page)
        return self._apply_session

    def open_job_page(self, url: str) -> None:
        # Plain GET navigation, so it is safe before the apply session's
        # network guard is armed. Without this step click_apply() runs
        # against whatever page login() left behind (the Naukri homepage),
        # which has no Apply button.
        # login() can return while Naukri's own post-login redirect is still in
        # flight (seen live: address still /nlogin/login). A goto() that lands on
        # top of it is aborted with a bare Playwright "Error", so let the redirect
        # finish and retry a couple of times before giving up.
        last_error: Exception | None = None
        for _attempt in range(_OPEN_JOB_ATTEMPTS):
            try:
                self._page.goto(url)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                try:
                    self._page.wait_for_timeout(_OPEN_JOB_RETRY_WAIT_MS)
                except Exception:  # noqa: BLE001
                    pass
        if last_error is not None:
            raise last_error
        try:
            self._page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:  # noqa: BLE001 - a slow settle must not abort; click_apply() has its own timeout
            pass

    def prepare_next_application(self) -> None:
        self._get_apply_session().reset_capture()

    def application_question_field_count(self) -> int:
        """Input fields visible on the application screen; -1 if it could not be read."""
        try:
            from naukri_agent.browser.apply_inspection import extract_application_ui

            return extract_application_ui(self._page).question_field_count
        except Exception:  # noqa: BLE001
            return -1

    def screenshot(self, path: Any) -> bool:
        try:
            self._page.screenshot(path=str(path))
            return True
        except Exception:  # noqa: BLE001 - a missing screenshot must never fail an application
            return False

    def detect_apply_type(self) -> str:
        from naukri_agent.browser.apply_workflow import detect_apply_type

        return detect_apply_type(self._page)

    def click_apply(self) -> None:
        self._get_apply_session().click_apply()

    def list_questions(self) -> list[ApplyQuestionPrompt]:
        return self._get_apply_session().list_questions()

    def submit_answer(self, control_id: str, answer: str) -> None:
        self._get_apply_session().submit_answer(control_id, answer)

    def skip_question(self, control_id: str) -> None:
        self._get_apply_session().skip_question(control_id)

    def submit_application(self) -> ApplySubmissionResult:
        return self._get_apply_session().submit_application()
