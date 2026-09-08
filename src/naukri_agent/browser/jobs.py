"""
Jobs: search results and job-listing/apply-workflow inspection.
Stage 1 is READ-ONLY — inspect_application_workflow OBSERVES what
controls exist on a job's apply flow but never clicks apply or submits
anything. Actually applying is Stage 2.
"""

from __future__ import annotations

import logging
from typing import Any

from naukri_agent.browser import selectors
from naukri_agent.browser.models import ApplicationWorkflowInspection, JobListingSummary

logger = logging.getLogger(__name__)


def search_jobs(page: Any, query: str, location: str = "") -> list[JobListingSummary]:
    url = selectors.SEARCH_URL_TEMPLATE.format(
        query=query.strip().lower().replace(" ", "-"),
        location=(location.strip().lower().replace(" ", "-") or "india"),
    )
    page.goto(url)
    page.wait_for_load_state("networkidle")

    results = []
    for card in page.query_selector_all(selectors.JOB_CARD):
        results.append(
            JobListingSummary(
                title=_text_or_none(card, selectors.JOB_CARD_TITLE),
                company=_text_or_none(card, selectors.JOB_CARD_COMPANY),
                location=_text_or_none(card, selectors.JOB_CARD_LOCATION),
                url=_attr_or_none(card, selectors.JOB_CARD_TITLE, "href"),
            )
        )
    return results


def inspect_application_workflow(page: Any, job_url: str) -> ApplicationWorkflowInspection:
    """
    Open a job listing and OBSERVE (never click through) how its
    apply workflow exposes resume selection/upload. Stage 1 steps 9-10.
    """
    page.goto(job_url)
    page.wait_for_load_state("networkidle")

    apply_present = _is_present(page, selectors.APPLY_BUTTON)
    resume_controls_present = _is_present(page, selectors.RESUME_SELECTION_CONTROLS)

    notes = []
    if not apply_present:
        notes.append(
            "No apply button matched the current (unverified) selector — "
            "run `naukri-agent inspect` and check the saved HTML manually."
        )

    return ApplicationWorkflowInspection(
        apply_button_present=apply_present,
        resume_selection_controls_present=resume_controls_present,
        notes=notes,
    )


def _text_or_none(element: Any, selector: str) -> str | None:
    try:
        el = element.query_selector(selector)
        return el.inner_text().strip() if el is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read text for %r: %s", selector, exc)
        return None


def _attr_or_none(element: Any, selector: str, attr: str) -> str | None:
    try:
        el = element.query_selector(selector)
        return el.get_attribute(attr) if el is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not read attribute %r for %r: %s", attr, selector, exc)
        return None


def _is_present(page: Any, selector: str) -> bool:
    try:
        return page.query_selector(selector) is not None
    except Exception as exc:  # noqa: BLE001
        logger.debug("query_selector(%r) raised %s; treating as not present", selector, exc)
        return False
