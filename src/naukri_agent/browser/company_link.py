"""
The employer's own address for a company-site job, read from the data Naukri's job page already loads.

The "Apply on company site" button has no link behind it, but Naukri's own page receives the address it will open
in the job's JSON (GET /jobapi/v4/job/<id>, field jobDetails.applyRedirectUrl). Checked on 2026-10-09 against four
real company-site jobs: it held the same address a person lands on after pressing the button (LG Soft on
Darwinbox, Syngenta on SmartRecruiters, ...).

Pressing the button is NOT an option: Naukri registers the press as an application, so the job shows "Applied" on
the account afterwards (seen on 2026-10-09, Syngenta, before and after). So this only LOOKS:
  * it opens the job page like any read, and watches the page's own GET responses;
  * it presses, types and submits nothing, and sends no request of its own;
  * it keeps only an address that points outside naukri.com.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_JOB_API_PATH = "/jobapi/v4/job/"
_POLL_MS = 300


@dataclass
class CompanySiteLink:
    url: str | None
    note: str


def _is_company_address(url: object) -> bool:
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        return False
    host = (urlsplit(url).hostname or "").lower()
    return bool(host) and not (host == "naukri.com" or host.endswith(".naukri.com"))


def _address_in(body: bytes | str | None) -> str | None:
    try:
        data = json.loads(body or b"")
        node = data.get("jobDetails") if isinstance(data, dict) else None
        url = node.get("applyRedirectUrl") if isinstance(node, dict) else None
    except Exception:  # noqa: BLE001 - not the JSON we are looking for
        return None
    return url.strip() if _is_company_address(url) else None


class ApplyLinkWatcher:
    """Watches a page's own GET responses for the job data that holds the employer's address. Start it BEFORE the
    page is opened; it only listens, and sends nothing."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self.url: str | None = None
        self.answered = False  # the job data arrived (with or without an address)
        self._started = False

    def _on_response(self, resp: Any) -> None:
        try:
            if resp.request.method != "GET" or _JOB_API_PATH not in urlsplit(resp.url).path:
                return
            self.answered = True
            found = _address_in(resp.body())
            if found and self.url is None:
                self.url = found
        except Exception:  # noqa: BLE001 - a response that cannot be read is simply not the one
            pass

    def start(self) -> "ApplyLinkWatcher":
        try:
            self._page.on("response", self._on_response)
            self._started = True
        except Exception:  # noqa: BLE001 - a page that cannot listen just yields no link
            pass
        return self

    def wait(self, max_ms: int) -> None:
        """Give the job data up to max_ms to arrive; returns at once when it already has."""
        waited = 0
        while self._started and not self.answered and waited < max_ms:
            try:
                self._page.wait_for_timeout(_POLL_MS)
            except Exception:  # noqa: BLE001
                break
            waited += _POLL_MS

    def stop(self) -> None:
        remove = getattr(self._page, "remove_listener", None)
        if self._started and remove is not None:
            try:
                remove("response", self._on_response)
            except Exception:  # noqa: BLE001
                pass
        self._started = False


def read_direct_apply_link(page: Any, job_url: str, *, wait_ms: int = 12000) -> CompanySiteLink:
    watcher = ApplyLinkWatcher(page).start()
    try:
        page.goto(job_url)
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        watcher.wait(wait_ms)
    finally:
        watcher.stop()

    if watcher.url:
        return CompanySiteLink(watcher.url, "read from the job data Naukri's own page loaded; nothing was pressed or applied")
    if watcher.answered:
        return CompanySiteLink(None, "Naukri's job data holds no company address for this job")
    return CompanySiteLink(None, f"Naukri's job data did not load within {wait_ms // 1000}s")
