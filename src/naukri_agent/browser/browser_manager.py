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
from typing import Any

from naukri_agent.config import Settings

logger = logging.getLogger(__name__)


class BrowserManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Any = None
        self._context: Any = None
        self.page: Any = None

    def __enter__(self) -> "BrowserManager":
        self.launch()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def launch(self) -> Any:
        # Imported lazily: the `playwright` package (and especially
        # its downloaded browser binaries) are only needed when a
        # browser is actually launched, not for importing this module
        # or for unit tests that never call launch().
        from playwright.sync_api import sync_playwright

        self.settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Launching browser (headless=%s)", self.settings.naukri_headless)

        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.settings.browser_profile_dir),
            headless=self.settings.naukri_headless,
        )
        self.page = self._context.new_page()
        return self.page

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._context = None
        self._playwright = None
        self.page = None
