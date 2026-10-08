"""
Read-only classification of how a loaded Naukri job page can be applied to.
Kept in its own import-light module so both the daily discovery fetch
(browser/jobs.py) and the apply workflow (browser/apply_workflow.py) can
use the same check without a circular import.
"""

from __future__ import annotations

from typing import Any

from naukri_agent.browser import selectors

APPLY_TYPE_NATIVE = "native"
APPLY_TYPE_COMPANY_SITE = "company_site"
APPLY_TYPE_NONE = "none"

_APPEAR_TIMEOUT_MS = 6000


def _any_visible(page: Any, selector: str) -> bool:
    try:
        return any(h.is_visible() for h in page.query_selector_all(selector))
    except Exception:  # noqa: BLE001 - a page we can't query is treated as "not found"
        return False


def detect_apply_type(page: Any) -> str:
    """
    Never clicks or fills. "native" if a visible Naukri Apply button
    exists, "company_site" if the only apply control sends the applicant
    to the employer's own website, else "none" (expired / already applied
    / removed / layout changed). Native wins if both somehow appear.
    """
    # Naukri draws the apply control a moment after the page loads (observed:
    # absent at domcontentloaded, present ~1s later), so wait briefly for either
    # button before concluding there is none. Best-effort: any failure here just
    # falls through to the immediate check below.
    if not (_any_visible(page, selectors.APPLY_BUTTON) or _any_visible(page, selectors.COMPANY_SITE_APPLY_BUTTON)):
        try:
            page.wait_for_selector(
                f"{selectors.APPLY_BUTTON}, {selectors.COMPANY_SITE_APPLY_BUTTON}",
                state="visible",
                timeout=_APPEAR_TIMEOUT_MS,
            )
        except Exception:  # noqa: BLE001 - nothing appeared in time (or the page can't wait): treat as absent
            pass
    if _any_visible(page, selectors.APPLY_BUTTON):
        return APPLY_TYPE_NATIVE
    if _any_visible(page, selectors.COMPANY_SITE_APPLY_BUTTON):
        return APPLY_TYPE_COMPANY_SITE
    return APPLY_TYPE_NONE
