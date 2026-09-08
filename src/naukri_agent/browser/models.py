"""
Structured result types returned by browser/login.py, profile.py, and
jobs.py. Nothing outside browser/ should need to know Playwright's
types — these are what the rest of the system (NaukriClient's
callers) actually sees.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, Field


class LoginStatus(str, enum.Enum):
    SUCCESS = "success"


class LoginResult(BaseModel):
    status: LoginStatus
    message: str
    current_url: str | None = None


class ResumeState(BaseModel):
    """
    Best-effort structured snapshot of the profile's resume section.
    Naukri's exact DOM has not yet been verified (Stage 1 — see
    browser/selectors.py) so treat any None/False field as
    "not yet determined", not "confirmed absent".
    """

    resume_filename: str | None = None
    last_updated_text: str | None = None
    upload_control_present: bool = False
    remove_control_present: bool = False


class JobListingSummary(BaseModel):
    """One row from a Naukri search-results page — inspection-only, not the full JobCreate shape."""

    title: str | None = None
    company: str | None = None
    location: str | None = None
    url: str | None = None


class ApplicationWorkflowInspection(BaseModel):
    """
    What was OBSERVED on a job's apply workflow — never an executed
    application. This is Stage 1's output for designing Stage 2, not
    something Stage 2 itself produces.
    """

    apply_button_present: bool = False
    resume_selection_controls_present: bool = False
    notes: list[str] = Field(default_factory=list)
