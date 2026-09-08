"""
Stage 1 inspection tool.

Run this against YOUR OWN Naukri account, on YOUR OWN machine, where
you have real network access and can complete a CAPTCHA/MFA challenge
yourself if one appears:

    naukri-agent inspect

This performs exactly Stage 1's steps: login, view profile/resume,
search jobs, open one listing, observe its apply workflow. Nothing is
modified — no upload, no removal, no application submission. HTML
snapshots, screenshots, and a JSON summary are written to
settings.inspection_output_dir (default ./inspection_output/, which
is gitignored). Share that output back so browser/selectors.py's
placeholders can be replaced with confirmed selectors.

Runs headed (headless=False) by default so you can see and solve any
CAPTCHA/MFA challenge directly in the browser window.

CAPTCHA/MFA handling (human-in-the-loop, never automated):
If login() raises NaukriCaptchaError/NaukriMfaError, this function
does NOT let the browser close. It saves a snapshot, prints an
instruction, and BLOCKS on wait_for_manual_completion (an injectable
callable — real input() by default) while still inside the
`with BrowserManager(...)` block, so the window stays open the whole
time. After the human completes the challenge and presses Enter, it
gives the page a moment to settle any redirect, then checks whether
the session is authenticated (never repeating login() if so). Only if
that check comes back negative does it retry login() — exactly once.
If the challenge is still present after that retry, the resulting
exception propagates to the normal fatal-error path below — snapshot
saved, browser closed, failed report returned. Any other
NaukriAutomationError (unexpected page, rate limit, ...) always goes
straight to that same fatal path; only CAPTCHA/MFA get the pause.

Playwright interaction failures (e.g. a timeout because a page never
reached the state the code expected) are caught at a narrow boundary
— specifically playwright.sync_api.Error and its subclasses, nothing
broader — and folded into the same structured failure report rather
than crashing with a raw traceback. A genuine programming error
(TypeError, AttributeError, ...) is NOT one of these and is left to
propagate normally, so real bugs stay distinguishable from expected
automation failures.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any, Callable

from naukri_agent.browser.browser_manager import BrowserManager
from naukri_agent.browser.exceptions import NaukriAutomationError, NaukriCaptchaError, NaukriMfaError
from naukri_agent.browser.models import LoginResult
from naukri_agent.browser.naukri_client import NaukriClient
from naukri_agent.config import Settings

logger = logging.getLogger(__name__)


def _save_snapshot(page: Any, out_dir: Path, name: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        (out_dir / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - snapshot is best-effort, never fatal
        logger.warning("Could not save HTML snapshot for %s: %s", name, exc)
    try:
        page.screenshot(path=str(out_dir / f"{name}.png"), full_page=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not capture screenshot for %s: %s", name, exc)


def _record_failure(report: dict, page: Any, out_dir: Path, exc: Exception) -> None:
    """
    Record a failed inspection into `report` and save a diagnostic
    snapshot. Shared by both the NaukriAutomationError path and the
    Playwright-interaction-error path in run_inspection — the
    exception's real type name is always preserved as-is (never
    relabeled), so the report is honest about whether the failure was
    one of our own detected states (CAPTCHA, unexpected page, ...) or
    a raw Playwright interaction failure (timeout, ...).
    """
    logger.error("Inspection stopped: %s: %s", type(exc).__name__, exc)
    report["stopped_at"] = report["steps"][-1]["step"] if report["steps"] else "login"
    report["error_type"] = type(exc).__name__
    report["error"] = str(exc)
    report["current_url"] = getattr(page, "url", None)
    _save_snapshot(page, out_dir, "stopped_state")


def _handle_login_challenge(
    client: NaukriClient,
    page: Any,
    out_dir: Path,
    exc: NaukriAutomationError,
    wait_for_manual_completion: Callable[[str], None],
) -> LoginResult:
    """
    Human-in-the-loop pause for a CAPTCHA/MFA challenge hit during
    login. Called from INSIDE the same `with BrowserManager(...)`
    block as the rest of the inspection — the browser is never closed
    here. Blocks on wait_for_manual_completion, gives the page a
    moment to settle any redirect the manual completion triggered,
    then checks whether the session is already authenticated before
    deciding whether login() actually needs to run again.

    Re-raises (via the retried login() call) if the challenge is
    still present — the caller's normal error handling takes it from
    there.
    """
    challenge_name = "CAPTCHA" if isinstance(exc, NaukriCaptchaError) else "MFA/OTP"
    logger.warning("%s challenge detected during login: %s", challenge_name, exc)
    _save_snapshot(page, out_dir, "challenge_detected")

    prompt = (
        f"\n[ACTION REQUIRED] A {challenge_name} challenge was detected during login.\n"
        f"The browser window is still open — complete it manually there.\n"
        f"Press Enter here once you're done: "
    )
    wait_for_manual_completion(prompt)

    # Give the page a moment to finish any redirect the manual
    # completion triggered before checking auth state — checking too
    # early is exactly what previously caused a false "not logged in"
    # read and an unnecessary, ultimately-broken retry.
    try:
        page.wait_for_load_state("networkidle")
    except Exception as settle_exc:  # noqa: BLE001 - best-effort settle; proceed to check regardless
        logger.debug(
            "wait_for_load_state after manual completion raised %s; checking auth state anyway",
            settle_exc,
        )

    already = client.check_already_logged_in()
    if already is not None:
        logger.info("Login already succeeded after manual %s completion; not repeating login.", challenge_name)
        return already

    logger.info("%s not yet resolved on the page; retrying login once.", challenge_name)
    return client.login()


def run_inspection(
    settings: Settings,
    search_query: str = "data scientist",
    wait_for_manual_completion: Callable[[str], None] | None = None,
) -> dict:
    """
    Run Stage 1's read-only sequence end to end.

    wait_for_manual_completion lets callers (tests, or a future
    non-interactive caller) supply their own blocking behavior;
    defaults to a real input() prompt for interactive use.
    """
    if wait_for_manual_completion is None:
        wait_for_manual_completion = input

    # Imported lazily (same pattern as BrowserManager.launch()) — only
    # needed once we're actually running a real inspection with a real
    # Playwright page, never at module import time or for tests that
    # only exercise the orchestration logic against fakes.
    from playwright.sync_api import Error as PlaywrightError

    out_dir = settings.inspection_output_dir / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    report: dict = {
        "started_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "steps": [],
        "completed": False,
    }

    with BrowserManager(settings) as browser:
        client = NaukriClient(browser.page, settings)

        try:
            try:
                login_result = client.login()
            except (NaukriCaptchaError, NaukriMfaError) as exc:
                login_result = _handle_login_challenge(
                    client, browser.page, out_dir, exc, wait_for_manual_completion
                )

            report["steps"].append({"step": "login", "status": login_result.status.value})
            _save_snapshot(browser.page, out_dir, "01_post_login")

            resume_state = client.get_profile_resume()
            report["steps"].append({"step": "profile_resume", "result": resume_state.model_dump()})
            _save_snapshot(browser.page, out_dir, "02_profile")

            listings = client.search_jobs(search_query)
            report["steps"].append({"step": "search_jobs", "result_count": len(listings)})
            _save_snapshot(browser.page, out_dir, "03_search_results")

            first_url = listings[0].url if listings else None
            if first_url:
                workflow = client.get_job(first_url)
                report["steps"].append(
                    {"step": "job_application_workflow", "job_url": first_url, "result": workflow.model_dump()}
                )
                _save_snapshot(browser.page, out_dir, "04_job_listing")
            else:
                report["steps"].append(
                    {"step": "job_application_workflow", "skipped": "no job listing URL captured"}
                )

            report["completed"] = True

        except NaukriAutomationError as exc:
            _record_failure(report, browser.page, out_dir, exc)
        except PlaywrightError as exc:
            _record_failure(report, browser.page, out_dir, exc)

    report["finished_at"] = datetime.datetime.now(datetime.UTC).isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["output_dir"] = str(out_dir)
    return report

