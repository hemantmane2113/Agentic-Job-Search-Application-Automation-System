"""
Exceptions for Naukri browser automation. All of these represent a
"stop and report" situation (Section: Authentication and safety) —
none of them are meant to be caught and retried around. If Naukri
presents a CAPTCHA, MFA challenge, rate limit, or an unexpected page,
automation stops; a human (or the calling tool, pausing for manual
intervention) handles it.
"""

from __future__ import annotations


class NaukriAutomationError(Exception):
    """Base class for all Naukri browser-automation errors."""


class NaukriLoginError(NaukriAutomationError):
    """Login failed for a reason other than CAPTCHA/MFA (e.g. missing credentials, wrong password)."""


class NaukriCaptchaError(NaukriAutomationError):
    """
    A CAPTCHA challenge was presented. Automation must never attempt
    to solve or circumvent it — raising this alone does not pause or
    resume anything, that's entirely up to the caller. See
    browser/inspection.py's run_inspection(), which specifically
    catches this to pause for manual completion while keeping the
    browser open (used by `naukri-agent inspect`); a caller that
    doesn't do that will simply stop when this is raised.
    """


class NaukriMfaError(NaukriAutomationError):
    """
    An MFA/OTP challenge was presented. Same handling contract as
    NaukriCaptchaError — see its docstring.
    """


class NaukriRateLimitError(NaukriAutomationError):
    """Naukri appears to be rate-limiting or blocking automated requests."""


class NaukriUnexpectedPageError(NaukriAutomationError):
    """
    The page didn't match any expected state. Naukri's UI may have
    changed since browser/selectors.py was last verified — see that
    file's module docstring for how to re-verify.
    """


class NaukriInteractionError(NaukriAutomationError):
    """
    A browser interaction (fill/click/goto/wait_for_*) failed in an
    expected-but-unplanned way — most commonly a Playwright timeout
    because a page didn't reach the state the code assumed it would.

    This exists as a distinct category from a genuine programming bug:
    it means "the automation hit a wall talking to the real site,"
    not "our code raised something it shouldn't have." See
    browser/inspection.py's run_inspection() for where Playwright's
    own exception types are caught, at a narrow boundary, and folded
    into this project's structured failure-reporting path — arbitrary
    exceptions (TypeError, AttributeError, ...) are deliberately left
    uncaught there so real bugs stay distinguishable from this.
    """
