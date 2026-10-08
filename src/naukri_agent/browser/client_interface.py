"""
JobBoardClient: the generic contract a job-board automation facade
must implement. NaukriClient is today's only implementation; this
exists so a future second platform can be added behind the same interface without a rewrite of
anything that consumes it — no second platform is implemented yet.

Plain ABC, not typing.Protocol, matching this codebase's only other
pluggable-backend precedent (llm/base.py's LLMProvider): callers never
construct a platform client directly from outside browser/, and a
missing method implementation should fail loudly at class-definition
time (TypeError), not silently at first use.

`prepare_application` is deliberately NOT part of this interface — it
is Naukri-specific historical cruft (a permanent NotImplementedError
sentinel predating the apply agent) that NaukriClient keeps as an
extra method; no future platform is obligated to define it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

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


class JobBoardClient(ABC):
    def __init__(self, page: Any, settings: Settings) -> None:
        self._page = page
        self._settings = settings

    # --- Stage 1: read-only discovery/inspection ---

    @abstractmethod
    def login(self) -> LoginResult: ...

    @abstractmethod
    def check_already_logged_in(self) -> LoginResult | None: ...

    @abstractmethod
    def get_profile_resume(self) -> ResumeState: ...

    @abstractmethod
    def search_jobs(self, query: str, location: str = "") -> list[JobListingSummary]: ...

    @abstractmethod
    def fetch_job_detail(self, url: str) -> JobDetail: ...

    @abstractmethod
    def get_job(self, url: str) -> ApplicationWorkflowInspection: ...

    @abstractmethod
    def extract_application_ui(self) -> ApplyUiInspection: ...

    # --- Phase 14: apply-write surface ---
    # Every method below is a real, potentially mutating browser action.
    # They must only ever be reached via orchestration/apply_runner.py's
    # human-gated flow — never called directly from an AI node or any
    # unattended pipeline.

    @abstractmethod
    def click_apply(self) -> None:
        """Click the job's Apply control. The one action that starts a
        real application workflow on the live site."""

    @abstractmethod
    def list_questions(self) -> list[ApplyQuestionPrompt]:
        """Return every question in the current application's
        questionnaire (mandatory and skippable), read without
        submitting anything."""

    @abstractmethod
    def submit_answer(self, control_id: str, answer: str) -> None:
        """Submit one human-approved answer for the given question."""

    @abstractmethod
    def skip_question(self, control_id: str) -> None:
        """Skip one skippable question without answering it."""

    @abstractmethod
    def submit_application(self) -> ApplySubmissionResult:
        """Final, irreversible submit. Must only ever be called after
        every question has been answered or skipped AND a human has
        explicitly confirmed submission — enforced by the caller
        (orchestration/apply_runner.py), not by this method itself."""
