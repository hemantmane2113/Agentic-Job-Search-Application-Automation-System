"""
Profile: inspects the candidate's Naukri profile, specifically the
resume section. Stage 1 is READ-ONLY — nothing here uploads, removes,
or otherwise modifies anything. Resume upload/refresh is Stage 2.
"""

from __future__ import annotations

import logging
from typing import Any

from naukri_agent.browser import selectors
from naukri_agent.browser.models import ResumeState

logger = logging.getLogger(__name__)


def get_profile_resume(page: Any) -> ResumeState:
    page.goto(selectors.PROFILE_URL)
    page.wait_for_load_state("networkidle")

    return ResumeState(
        resume_filename=_text_or_none(page, selectors.RESUME_FILENAME),
        last_updated_text=_text_or_none(page, selectors.RESUME_LAST_UPDATED),
        upload_control_present=_is_present(page, selectors.RESUME_UPLOAD_BUTTON),
        remove_control_present=_is_present(page, selectors.RESUME_REMOVE_BUTTON),
    )


def _text_or_none(page: Any, selector: str) -> str | None:
    try:
        el = page.query_selector(selector)
        return el.inner_text().strip() if el is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read text for %r: %s", selector, exc)
        return None


def _is_present(page: Any, selector: str) -> bool:
    try:
        return page.query_selector(selector) is not None
    except Exception as exc:  # noqa: BLE001
        logger.debug("query_selector(%r) raised %s; treating as not present", selector, exc)
        return False
