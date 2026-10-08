"""
Profile: inspects the candidate's Naukri profile, specifically the
resume section. Stage 1 is READ-ONLY — nothing here uploads, removes,
or otherwise modifies anything. Resume upload/refresh is Stage 2.
"""

from __future__ import annotations

import logging
from typing import Any

from naukri_agent.browser import selectors
from naukri_agent.browser.models import ResumeState, ResumeUploadResult

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


_UPLOAD_SETTLE_MS = 8000
_FILENAME_WAIT_MS = 15000


def _norm(name: str | None) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _read_resume_section(page: Any) -> tuple[str | None, str | None]:
    return _text_or_none(page, selectors.RESUME_FILENAME), _text_or_none(page, selectors.RESUME_LAST_UPDATED)


def upload_resume(page: Any, path: Any) -> ResumeUploadResult:
    """
    WRITE: replace the Naukri profile's resume with the file at `path`, which Naukri also
    counts as a profile update ("last updated" refreshes). The only action is setting the
    resume file input; nothing else on the page is touched. The result is checked by
    reloading the profile and reading the resume section back - never assumed.
    """
    from pathlib import Path

    file_path = Path(path)
    page.goto(selectors.PROFILE_URL)
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    page.wait_for_selector(selectors.RESUME_UPLOAD_BUTTON, state="attached", timeout=_FILENAME_WAIT_MS)
    before_name, before_updated = _read_resume_section(page)

    page.set_input_files(selectors.RESUME_UPLOAD_BUTTON, str(file_path))
    page.wait_for_timeout(_UPLOAD_SETTLE_MS)  # let the upload request finish before leaving the page

    page.reload()
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    try:
        page.wait_for_selector(selectors.RESUME_FILENAME, state="attached", timeout=_FILENAME_WAIT_MS)
    except Exception as exc:  # noqa: BLE001 - reported below as unverified
        logger.debug("resume filename not visible after reload: %s", exc)
    after_name, after_updated = _read_resume_section(page)

    verified = bool(after_name) and _norm(file_path.stem) in _norm(after_name)
    note = None if verified else "the profile did not show the uploaded file name after a reload"
    return ResumeUploadResult(
        before_filename=before_name, before_updated=before_updated,
        after_filename=after_name, after_updated=after_updated, verified=verified, note=note,
    )


def ensure_resume(page: Any, path: Any) -> ResumeUploadResult:
    """
    Make sure the profile's resume IS `path`'s file, uploading only if it is not. Naukri applies
    with the profile's current resume, so this runs right before an application, to make that
    application carry the resume chosen for the job's role. Reads first, so a resume that is
    already in place costs one page load and changes nothing.
    """
    from pathlib import Path

    file_path = Path(path)
    page.goto(selectors.PROFILE_URL)
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    try:
        page.wait_for_selector(selectors.RESUME_FILENAME, state="attached", timeout=_FILENAME_WAIT_MS)
    except Exception as exc:  # noqa: BLE001 - fall through: upload_resume reports what it sees
        logger.debug("resume filename not visible before check: %s", exc)
    name, updated = _read_resume_section(page)
    if name and _norm(file_path.stem) in _norm(name):
        return ResumeUploadResult(
            before_filename=name, before_updated=updated, after_filename=name, after_updated=updated,
            verified=True, changed=False, note="already on the profile",
        )
    return upload_resume(page, file_path)
