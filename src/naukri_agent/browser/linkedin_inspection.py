"""
LinkedIn L1: read-only inspection.

    naukri-agent linkedin-inspect

Purpose: capture the REAL LinkedIn DOM (search results, a job page, the apply
controls) so later phases can use verified selectors instead of guesses - the
same job `inspect` did for Naukri. Nothing is modified.

Why this is deliberately gentler than the Naukri inspection:
  * LinkedIn prohibits automated access and detects it, and a restricted account
    costs far more than a Naukri one. So the footprint is tiny: the feed, ONE
    search page, ONE job page, with a few seconds of pause between navigations.
  * The tool never touches the login form. The person logs in themselves in the
    window it opens, so credentials are never read, typed or stored by this code.
  * Any login wall / security checkpoint / CAPTCHA is left to the person. The
    tool waits for them; it never tries to get past one.
  * Once logged in, every non-GET/HEAD/OPTIONS request is aborted (default-deny),
    so even a bug cannot submit anything. Aborted requests are listed (method +
    path only) in the report.
  * The tool never clicks anything. It only navigates (URL loads) and reads.

LinkedIn uses its OWN persistent browser profile (settings.linkedin_profile_dir),
never Naukri's. Output goes to settings.inspection_output_dir (gitignored): saved
HTML contains your name and feed, so do not share it unredacted.
"""

from __future__ import annotations

import datetime
import json
import logging
import random
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from naukri_agent.browser import selectors as sel
from naukri_agent.browser.browser_manager import BrowserManager
from naukri_agent.browser.inspection import _safe_page_url, _save_snapshot
from naukri_agent.config import Settings

logger = logging.getLogger(__name__)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_BLOCKED_CAP = 50
_PACE_RANGE_S = (2.0, 5.0)

# Probes take their selectors as an argument so this module stays free of literals.
SEARCH_PROBE_JS = """
(s) => {
  const links = Array.from(document.querySelectorAll(s.jobLink));
  const ids = [];
  for (const a of links) {
    const m = (a.getAttribute('href') || '').match(/\\/jobs\\/view\\/(\\d+)/);
    if (m && !ids.includes(m[1])) ids.push(m[1]);
  }
  const first = links[0] || null;
  const card = first ? first.closest('li') : null;
  const cls = (e) => e ? String(e.className || '').slice(0, 200) : null;
  return {
    page_title: document.title,
    link_count: links.length,
    distinct_job_ids: ids.length,
    sample_job_ids: ids.slice(0, 10),
    first_card_tag: card ? card.tagName.toLowerCase() : null,
    first_card_class: cls(card),
    list_class: card ? cls(card.parentElement) : null,
  };
}
"""

JOB_PROBE_JS = """
(s) => {
  const h1 = document.querySelector(s.title);
  const text = (e) => ((e.innerText || '') + ' ' + (e.getAttribute('aria-label') || '')).trim();
  const controls = Array.from(document.querySelectorAll(s.controls)).filter((e) => /apply/i.test(text(e)));
  const cls = (e) => String(e.className || '').slice(0, 120);
  return {
    page_title: document.title,
    title: h1 ? h1.innerText.trim().slice(0, 150) : null,
    easy_apply_present: controls.some((e) => /easy apply/i.test(text(e))),
    apply_controls: controls.slice(0, 10).map((e) => ({
      tag: e.tagName.toLowerCase(),
      text: (e.innerText || '').trim().slice(0, 40),
      aria_label: (e.getAttribute('aria-label') || '').slice(0, 80),
      id: e.id || null,
      class: cls(e),
      href_path: e.tagName === 'A' && e.href ? new URL(e.href).pathname : null,
    })),
    applied_word_on_page: /\\bApplied\\b/.test(document.body ? document.body.innerText : ''),
  };
}
"""


class LinkedInInspectionError(Exception):
    """An expected stop (login not completed, interactive terminal missing, ...)."""


class ReadOnlyGuard:
    """Context-level default-deny: once armed, any request that is not GET/HEAD/OPTIONS is aborted."""

    def __init__(self) -> None:
        self.armed = False
        self.blocked: list[dict[str, str]] = []
        self.blocked_total = 0

    def attach(self, context: Any) -> None:
        context.route("**/*", self._handle)

    def arm(self) -> None:
        self.armed = True

    def _handle(self, route: Any) -> None:
        request = route.request
        method = str(request.method).upper()
        if self.armed and method not in _SAFE_METHODS:
            self.blocked_total += 1
            if len(self.blocked) < _BLOCKED_CAP:
                self.blocked.append({"method": method, "path": urlsplit(request.url).path})
            route.abort()
            return
        route.continue_()


def _url_has(page: Any, fragments: tuple[str, ...]) -> bool:
    url = (_safe_page_url(page) or "").lower()
    return any(f in url for f in fragments)


def _needs_human(page: Any) -> str | None:
    if _url_has(page, sel.LINKEDIN_CHALLENGE_URL_FRAGMENTS):
        return "a LinkedIn security check / CAPTCHA"
    if _url_has(page, sel.LINKEDIN_LOGIN_WALL_URL_FRAGMENTS):
        return "logging in to LinkedIn"
    return None


def _settle(page: Any) -> None:
    try:
        page.wait_for_load_state("load", timeout=15000)
    except Exception as exc:  # noqa: BLE001 - best-effort; LinkedIn is a busy SPA
        logger.debug("load-state wait ignored: %s", exc)


def _ensure_logged_in(page: Any, out_dir: Path, wait: Callable[[str], Any], report: dict) -> None:
    """Go to the feed; if LinkedIn wants a human, wait for them (once), then re-check."""
    page.goto(sel.LINKEDIN_FEED_URL)
    _settle(page)
    reason = _needs_human(page)
    if reason is None:
        report["steps"].append({"step": "login", "status": "already_logged_in"})
        return
    _save_snapshot(page, out_dir, "00_needs_human")
    wait(
        f"\n[ACTION REQUIRED] LinkedIn is asking for {reason}.\n"
        "Complete it yourself in the browser window (this tool never types your credentials\n"
        "or solves checks). Press Enter here once you are on your LinkedIn feed: "
    )
    page.goto(sel.LINKEDIN_FEED_URL)
    _settle(page)
    reason = _needs_human(page)
    if reason is not None:
        raise LinkedInInspectionError(f"still waiting on {reason} after the pause; stopping without retrying")
    report["steps"].append({"step": "login", "status": "completed_by_user"})


def _search_url(query: str, location: str) -> str:
    return f"{sel.LINKEDIN_JOBS_SEARCH_URL}?{urlencode({'keywords': query, 'location': location})}"


def run_linkedin_inspection(
    settings: Settings,
    query: str = "data scientist",
    location: str = "India",
    wait_for_manual_completion: Callable[[str], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    browser_factory: Callable[[Settings], Any] | None = None,
) -> dict:
    wait = wait_for_manual_completion or input
    make_browser = browser_factory or (lambda s: BrowserManager(s, profile_dir_override=s.linkedin_profile_dir))

    out_dir = settings.inspection_output_dir / ("linkedin_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    report: dict = {
        "started_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "steps": [],
        "completed": False,
    }
    guard = ReadOnlyGuard()

    def pace() -> None:
        sleep(random.uniform(*_PACE_RANGE_S))

    page: Any = None
    try:
        from playwright.sync_api import Error as PlaywrightError
    except Exception:  # noqa: BLE001 - playwright missing: only matters when really launching
        PlaywrightError = ()  # type: ignore[assignment,misc]

    try:
        with make_browser(settings) as browser:
            page = browser.page
            guard.attach(browser.context)  # attached unarmed: the person's login POSTs must pass
            try:
                _ensure_logged_in(page, out_dir, wait, report)
                guard.arm()
                _save_snapshot(page, out_dir, "01_feed")

                pace()
                page.goto(_search_url(query, location))
                _settle(page)
                _save_snapshot(page, out_dir, "02_search_results")
                probe = page.evaluate(SEARCH_PROBE_JS, {"jobLink": sel.LINKEDIN_JOB_LINK})
                report["steps"].append({"step": "search", "query": query, "location": location, "probe": probe})

                ids = (probe or {}).get("sample_job_ids") or []
                if ids:
                    pace()
                    job_url = sel.LINKEDIN_JOB_VIEW_URL.format(job_id=ids[0])
                    page.goto(job_url)
                    _settle(page)
                    _save_snapshot(page, out_dir, "03_job_page")
                    job_probe = page.evaluate(
                        JOB_PROBE_JS,
                        {"title": sel.LINKEDIN_JOB_TITLE, "controls": sel.LINKEDIN_APPLY_CONTROL_CANDIDATES},
                    )
                    report["steps"].append({"step": "job_page", "job_id": ids[0], "probe": job_probe})
                else:
                    report["steps"].append({"step": "job_page", "skipped": "no job links found on the search page"})
                report["completed"] = True
            except LinkedInInspectionError as exc:
                _record(report, page, out_dir, exc)
            except EOFError:
                report["error_type"] = "EOFError"
                report["error"] = "needs an interactive terminal (could not wait for you to log in)"
            except KeyboardInterrupt:
                report["error_type"] = "KeyboardInterrupt"
                report["error"] = "interrupted"
                raise
            except PlaywrightError as exc:  # type: ignore[misc]
                _record(report, page, out_dir, exc)
    finally:
        report["blocked_mutating_requests"] = {"total": guard.blocked_total, "sample": guard.blocked}
        report["finished_at"] = datetime.datetime.now(datetime.UTC).isoformat()
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["output_dir"] = str(out_dir)
    return report


def _record(report: dict, page: Any, out_dir: Path, exc: Exception) -> None:
    report["stopped_at"] = report["steps"][-1]["step"] if report["steps"] else "login"
    report["error_type"] = type(exc).__name__
    report["error"] = str(exc)
    report["current_url"] = re.sub(r"\?.*$", "", _safe_page_url(page) or "") or None  # never keep a query string
    if page is not None:
        _save_snapshot(page, out_dir, "stopped_state")
