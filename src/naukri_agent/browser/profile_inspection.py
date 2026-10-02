"""
Profile-edit inspection tool — a PREREQUISITE for a future resume/
profile "touch to refresh last-updated" feature, not that feature
itself. Naukri's profile-EDIT page DOM has never been inspected by this
codebase: browser/profile.py only ever reads the resume SECTION of the
read-only profile view, never an edit surface.

Run this against YOUR OWN Naukri account, on YOUR OWN machine, same as
`inspect`/`inspect-apply`:

    naukri-agent inspect-profile-edit

Strictly READ-ONLY: full-page HTML, screenshot, and a generic
enumeration of inputs/selects/textareas/buttons on whatever the
profile-edit surface turns out to be. No fill, no click beyond
navigation, no save — this tool never operates any control it finds.

Defaults to an isolated, disposable browser profile (like
`inspect-apply`, unlike plain `inspect`'s persistent-profile default) —
a safer default since this surface's behavior under exploration is
completely unknown.

CAPTCHA/MFA handling and the Playwright-error boundary are reused
UNCHANGED from Stage 1 (browser/inspection.py) — same human-in-the-loop
pause, same narrow playwright.sync_api.Error catch so a real
programming bug stays distinguishable from an expected automation
failure.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any, Callable

from naukri_agent.browser import selectors
from naukri_agent.browser.browser_manager import BrowserManager
from naukri_agent.browser.exceptions import NaukriAutomationError, NaukriCaptchaError, NaukriMfaError
from naukri_agent.browser.inspection import _handle_login_challenge, _record_failure, _save_snapshot
from naukri_agent.browser.models import ProfileEditControl, ProfileEditInspection
from naukri_agent.browser.naukri_client import NaukriClient
from naukri_agent.config import Settings

logger = logging.getLogger(__name__)


def _attr(el: Any, name: str) -> str | None:
    try:
        return el.get_attribute(name)
    except Exception:  # noqa: BLE001
        return None


def _safe_eval(el: Any, script: str) -> Any:
    try:
        return el.evaluate(script)
    except Exception as exc:  # noqa: BLE001
        logger.debug("evaluate(%r) raised %s", script, exc)
        return None


def _describe_control(el: Any) -> ProfileEditControl:
    tag = (_safe_eval(el, "e => e.tagName && e.tagName.toLowerCase()") or "").lower() or "unknown"
    try:
        text = (el.inner_text() or "").strip()
    except Exception:  # noqa: BLE001
        text = ""
    return ProfileEditControl(
        tag=tag,
        type=_attr(el, "type"),
        element_id=_attr(el, "id"),
        name=_attr(el, "name"),
        placeholder=_attr(el, "placeholder"),
        aria_label=_attr(el, "aria-label"),
        label_text=text or None,
        disabled=_attr(el, "disabled") is not None,
    )


def _looks_like_save_control(control: ProfileEditControl) -> bool:
    haystack = " ".join(
        filter(None, [control.label_text, control.aria_label, control.name, control.element_id])
    ).lower()
    return any(hint in haystack for hint in selectors.PROFILE_EDIT_SAVE_BUTTON_HINTS)


def _extract_profile_edit_ui(page: Any) -> ProfileEditInspection:
    """READ the profile-edit surface. Never clicks, fills, or navigates
    beyond the initial goto(). Absence of a field means "not observed in
    this capture", never "confirmed absent" — same caveat as every
    other Stage 1-style inspection result in this codebase."""
    found_roots: list[str] = []
    all_controls: list[ProfileEditControl] = []
    notes: list[str] = []

    for sel in selectors.PROFILE_EDIT_SECTION_ROOT_CANDIDATES:
        try:
            matches = page.query_selector_all(sel)
        except Exception as exc:  # noqa: BLE001
            logger.debug("query_selector_all(%r) raised %s; skipping", sel, exc)
            continue
        if not matches:
            continue
        found_roots.append(sel)
        for el in matches:
            try:
                controls = el.query_selector_all("input, select, textarea, button")
            except Exception as exc:  # noqa: BLE001
                logger.debug("query_selector_all on candidate root raised %s", exc)
                continue
            all_controls.extend(_describe_control(c) for c in controls)

    if not found_roots:
        notes.append(
            "No candidate profile-edit section root matched "
            "selectors.PROFILE_EDIT_SECTION_ROOT_CANDIDATES."
        )

    save_candidates = [c for c in all_controls if _looks_like_save_control(c)]
    if all_controls and not save_candidates:
        notes.append("Found controls but none matched a save/update text hint.")

    return ProfileEditInspection(
        section_roots_found=found_roots,
        controls=all_controls,
        save_control_candidates=save_candidates,
        notes=notes,
    )


def run_profile_edit_inspection(
    settings: Settings,
    *,
    isolated_profile: bool = True,
    wait_for_manual_completion: Callable[[str], None] | None = None,
) -> dict:
    """
    Run the profile-edit inspection end to end. wait_for_manual_completion
    lets callers (tests) supply their own blocking behavior; defaults to
    a real input() prompt for interactive use — same contract as
    run_inspection()/run_apply_inspection().
    """
    if wait_for_manual_completion is None:
        wait_for_manual_completion = input

    from playwright.sync_api import Error as PlaywrightError

    out_dir = settings.inspection_output_dir / (
        "profile_edit_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    profile_dir: Path | None = (
        settings.inspection_output_dir / "_profile_edit_session_profiles"
        / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        if isolated_profile
        else None
    )

    report: dict = {
        "kind": "profile_edit_inspection",
        "started_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "isolated_profile": bool(isolated_profile),
        "steps": [],
        "completed": False,
    }

    with BrowserManager(settings, profile_dir_override=profile_dir) as browser:
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

            browser.page.goto(selectors.PROFILE_EDIT_URL)
            browser.page.wait_for_load_state("networkidle")
            inspection = _extract_profile_edit_ui(browser.page)
            report["steps"].append({"step": "profile_edit_ui", "result": inspection.model_dump()})
            _save_snapshot(browser.page, out_dir, "02_profile_edit")

            report["completed"] = True

        except NaukriAutomationError as exc:
            _record_failure(report, browser.page, out_dir, exc)
        except PlaywrightError as exc:
            _record_failure(report, browser.page, out_dir, exc)

    report["finished_at"] = datetime.datetime.now(datetime.UTC).isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["output_dir"] = str(out_dir)
    return report
