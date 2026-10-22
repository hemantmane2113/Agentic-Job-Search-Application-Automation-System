"""
BrowserManager: owns the Playwright lifecycle (launch, persistent
context, page, close). Nothing else in this package should call
Playwright's launch APIs directly — get a page from here.

Uses a PERSISTENT browser context (a real user-data directory, not an
ephemeral in-memory session) so a logged-in session can survive
between runs — reducing how often CAPTCHA/MFA gets triggered at all,
per Section 20's guidance. The profile directory is gitignored
(personal session data).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from naukri_agent.config import Settings
from naukri_agent.safe_error import safe_error_text

logger = logging.getLogger(__name__)


class BrowserManager:
    def __init__(self, settings: Settings, profile_dir_override: Path | None = None) -> None:
        self.settings = settings
        # Stage 1.5 (`inspect-apply`) passes an isolated, disposable
        # profile dir here so a sensitive post-Apply inspection never
        # runs on the normal persistent session. Everything else leaves
        # this None and gets settings.browser_profile_dir.
        self._profile_dir_override = profile_dir_override
        self._playwright: Any = None
        self._context: Any = None
        # Public handle to the Playwright BrowserContext. Preferred over
        # `page` for context-wide concerns like request routing, which
        # then also covers popup windows the page may open.
        self.context: Any = None
        self.page: Any = None
        self._lock_path: Path | None = None  # set while this manager holds the shared-profile lock

    def _take_profile_lock(self) -> None:
        """Wait for any other command using the shared Naukri profile to finish (see browser/profile_lock.py). Not used
        for a disposable profile (profile_dir_override), which nobody else can be using."""
        if self._profile_dir_override is not None or not self.settings.browser_lock_enabled:
            return
        from naukri_agent.browser import profile_lock

        profile_lock.acquire(self.settings.browser_lock_file, self.settings.browser_lock_wait_minutes * 60)
        self._lock_path = self.settings.browser_lock_file

    def _release_profile_lock(self) -> None:
        path, self._lock_path = self._lock_path, None
        if path is not None:
            from naukri_agent.browser import profile_lock

            profile_lock.release(path)

    def __enter__(self) -> "BrowserManager":
        self.launch()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # close() is best-effort and never raises (see below), so it can
        # never mask an exception already propagating out of the `with`
        # body — e.g. a CAPTCHA the user abandoned by closing the
        # browser window, which leaves the Playwright connection dead.
        # Returns None -> the original exception is re-raised normally.
        self.close()

    def launch(self) -> Any:
        # Imported lazily: the `playwright` package (and especially
        # its downloaded browser binaries) are only needed when a
        # browser is actually launched, not for importing this module
        # or for unit tests that never call launch().
        from playwright.sync_api import sync_playwright

        profile_dir = self._profile_dir_override or self.settings.browser_profile_dir
        profile_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Launching browser (headless=%s, profile=%s)",
            self.settings.naukri_headless,
            profile_dir,
        )

        self._take_profile_lock()
        try:
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=self.settings.naukri_headless,
            )
            self.context = self._context
            self.page = self._context.new_page()
        except Exception as exc:
            # The error type alone ("Error") left a failed launch undiagnosable; the LOG FILE gets a scrubbed first line.
            logger.error("browser launch failed: %s", safe_error_text(exc, self.settings))
            self.close()  # also lets go of the profile lock, so the next command is not kept waiting
            raise
        return self.page

    def close(self) -> None:
        """
        Best-effort teardown. MUST NOT raise: it runs from __exit__
        while another exception may be propagating, and a dead/closed
        Playwright driver connection (browser window closed by the user
        mid-CAPTCHA, driver dropped, ...) would otherwise throw
        "Connection closed while reading from the driver" and mask the
        real error. State is cleared first, so this is also idempotent
        and a partial failure still leaves the manager in a clean
        "closed" state.
        """
        context, playwright = self._context, self._playwright
        self._context = None
        self._playwright = None
        self.context = None
        self.page = None
        try:
            self._release_profile_lock()
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            logger.debug("BrowserManager.close(): releasing the profile lock ignored: %s", type(exc).__name__)

        if context is not None:
            try:
                context.close()
            except Exception as exc:  # noqa: BLE001 - cleanup must not raise
                logger.debug("BrowserManager.close(): context.close() ignored: %s", exc)
        if playwright is not None:
            try:
                playwright.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup must not raise
                logger.debug("BrowserManager.close(): playwright.stop() ignored: %s", exc)
