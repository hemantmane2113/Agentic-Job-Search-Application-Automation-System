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


def read_direct_apply_link(page: Any, job_url: str, *, wait_ms: int = 12000) -> CompanySiteLink:
    state: dict[str, Any] = {"url": None, "answered": False}

    def on_response(resp: Any) -> None:
        try:
            if resp.request.method != "GET" or _JOB_API_PATH not in urlsplit(resp.url).path:
                return
            state["answered"] = True
            found = _address_in(resp.body())
            if found and state["url"] is None:
                state["url"] = found
        except Exception:  # noqa: BLE001 - a response that cannot be read is simply not the one
            pass

    page.on("response", on_response)
    try:
        page.goto(job_url)
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        waited = 0
        while not state["answered"] and waited < wait_ms:
            page.wait_for_timeout(_POLL_MS)
            waited += _POLL_MS
    finally:
        remove = getattr(page, "remove_listener", None)
        if remove is not None:
            try:
                remove("response", on_response)
            except Exception:  # noqa: BLE001
                pass

    if state["url"]:
        return CompanySiteLink(state["url"], "read from the job data Naukri's own page loaded; nothing was pressed or applied")
    if state["answered"]:
        return CompanySiteLink(None, "Naukri's job data holds no company address for this job")
    return CompanySiteLink(None, f"Naukri's job data did not load within {wait_ms // 1000}s")
