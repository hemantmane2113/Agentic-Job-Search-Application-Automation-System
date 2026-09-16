"""
Jobs: search results and job-listing/apply-workflow inspection.
Stage 1 is READ-ONLY — inspect_application_workflow OBSERVES what
controls exist on a job's apply flow but never clicks apply or submits
anything. Actually applying is Stage 2.
"""

from __future__ import annotations

import html as _html
import json as _json
import logging
import re as _re
from typing import Any

from naukri_agent.browser import selectors
from naukri_agent.browser.models import (
    ApplicationWorkflowInspection,
    JobDetail,
    JobListingSummary,
    KeySkillChip,
)

logger = logging.getLogger(__name__)

_DETAIL_SETTLE_TIMEOUT_MS = 15000


def fetch_job_detail(page: Any, url: str) -> JobDetail:
    """
    Open a job's public listing DETAIL page and READ its content.

    Strategy: parse the server-rendered schema.org ``JobPosting``
    (``<script type="application/ld+json">``) first — it is in the
    initial HTML before hydration and its shape is a public standard,
    not a Naukri build hash — then fall back, per missing field, to the
    hashed-class visible DOM.

    READ-ONLY: only navigates and reads (goto / wait / query_selector /
    element text). Never clicks, fills, submits, or touches the apply
    workflow. Every field degrades to None independently; a parse miss
    never raises. A ``goto`` failure returns a bare ``JobDetail(url=...)``.
    """
    try:
        page.goto(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_job_detail: goto(%s) raised %s", url, exc)
        return JobDetail(url=url)
    for state in ("domcontentloaded", "load"):
        try:
            page.wait_for_load_state(state, timeout=_DETAIL_SETTLE_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001
            logger.debug("fetch_job_detail: wait_for_load_state(%r) raised %s", state, exc)
    # Bounded wait for the JD to actually be present before reading. The
    # ld+json block is usually already in the domcontentloaded HTML; this
    # mainly covers the visible-DOM fallback / a late structured-data
    # injection. A timeout here is non-fatal — fields still degrade to None.
    try:
        page.wait_for_selector(
            selectors.JOB_DETAIL_READY, state="attached", timeout=_DETAIL_SETTLE_TIMEOUT_MS
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_job_detail: JD content did not settle for %s (%s)", url, exc)

    ld = _read_job_posting_ld_json(page)

    title = _clean(ld.get("title")) if ld else None
    company = _ld_company(ld)
    location = _ld_location(ld)
    salary_text = _ld_salary(ld)
    posted = _clean(ld.get("datePosted")) if ld else None
    description = _html_to_text(ld.get("description")) if ld else None

    # Per-field fallback to the visible (hashed-class) DOM.
    title = title or _first_text(page, selectors.JOB_DETAIL_TITLE)
    company = company or _first_text(page, selectors.JOB_DETAIL_COMPANY)
    location = location or _first_text(page, selectors.JOB_DETAIL_LOCATION)
    salary_text = salary_text or _first_text(page, selectors.JOB_DETAIL_SALARY)
    posted = posted or _first_text(page, selectors.JOB_DETAIL_STATS)
    if not description:
        description = _first_text(page, selectors.JOB_DETAIL_DESCRIPTION)

    # Experience: the visible DOM carries the full "X - Y years" range;
    # ld+json only carries a minimum in months.
    experience_text = _first_text(page, selectors.JOB_DETAIL_EXPERIENCE) or _ld_experience(ld)

    # Raw skill evidence (2026-09-11 hybrid-skill-source phase). Both
    # degrade to None independently and NEVER fail the job: ld_json_skills
    # is read from the same already-fetched `ld` dict (no extra network
    # cost); key_skills_dom is a best-effort DOM read that is allowed to
    # come back None on any selector miss/drift, exactly like every
    # other field above.
    ld_json_skills = _ld_skills(ld)
    key_skills_dom = _key_skills_dom(page)

    return JobDetail(
        url=url,
        title=title or None,
        company=company or None,
        location=location or None,
        salary_text=salary_text or None,
        experience_text=experience_text or None,
        description=description or None,
        posted_date_text=posted or None,
        ld_json_skills=ld_json_skills,
        key_skills_dom=key_skills_dom,
    )


def _first_text(page: Any, selector: str) -> str | None:
    try:
        el = page.query_selector(selector)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_job_detail: query_selector(%r) raised %s", selector, exc)
        return None
    if el is None:
        return None
    try:
        text = (el.inner_text() or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_job_detail: inner_text for %r raised %s", selector, exc)
        return None
    return text or None


# --- schema.org JobPosting (server-rendered ld+json) ------------------


def _read_job_posting_ld_json(page: Any) -> dict | None:
    """Return the first ``JobPosting`` object from any
    ``<script type="application/ld+json">`` on the page, or None. Never
    raises: a missing tag, unreadable node, or bad JSON is skipped."""
    try:
        nodes = page.query_selector_all(selectors.JOB_DETAIL_LD_JSON)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_job_detail: ld+json query_selector_all raised %s", exc)
        return None
    for node in nodes or []:
        raw = None
        for getter in ("text_content", "inner_text"):
            fn = getattr(node, getter, None)
            if fn is None:
                continue
            try:
                raw = fn()
            except Exception:  # noqa: BLE001
                raw = None
            if raw:
                break
        if not raw:
            continue
        try:
            data = _json.loads(raw)
        except Exception:  # noqa: BLE001 - untrusted page content
            continue
        found = _find_job_posting(data)
        if found is not None:
            return found
    return None


def _find_job_posting(data: Any) -> dict | None:
    if isinstance(data, dict):
        t = data.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            return data
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                found = _find_job_posting(item)
                if found is not None:
                    return found
        return None
    if isinstance(data, list):
        for item in data:
            found = _find_job_posting(item)
            if found is not None:
                return found
    return None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = _re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _ld_company(ld: dict | None) -> str | None:
    if not ld:
        return None
    org = ld.get("hiringOrganization")
    if isinstance(org, dict):
        return _clean(org.get("name"))
    if isinstance(org, str):
        return _clean(org)
    return None


def _ld_location(ld: dict | None) -> str | None:
    if not ld:
        return None
    names: list[str] = []

    def _collect(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _collect(item)
        elif isinstance(node, dict):
            addr = node.get("address", node)
            if isinstance(addr, list):
                _collect(addr)
            elif isinstance(addr, dict):
                city = addr.get("addressLocality")
                if isinstance(city, list):
                    names.extend(str(c) for c in city)
                elif isinstance(city, str):
                    names.append(city)

    _collect(ld.get("jobLocation"))
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        c = _clean(name)
        if c and c != "-" and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return ", ".join(out) or None


def _ld_salary(ld: dict | None) -> str | None:
    if not ld:
        return None
    base = ld.get("baseSalary")
    if not isinstance(base, dict):
        return None
    currency = _clean(base.get("currency")) or ""
    value = base.get("value")
    if isinstance(value, dict):
        literal = value.get("value")
        if isinstance(literal, str) and _clean(literal):
            return _clean(literal)
        lo, hi = value.get("minValue"), value.get("maxValue")
        if lo or hi:
            unit = _clean(value.get("unitText")) or ""
            return _clean(f"{currency} {lo or '?'}-{hi or '?'} {unit}")
    elif isinstance(value, str):
        return _clean(value)
    return None


def _ld_experience(ld: dict | None) -> str | None:
    if not ld:
        return None
    req = ld.get("experienceRequirements")
    months_raw = req.get("monthsOfExperience") if isinstance(req, dict) else None
    # Naukri's real captured output emits this as a STRING (e.g. "24"),
    # not a number — a real page (inspection_output/20260909_023839/
    # 04_job_listing.html) was found returning None here silently
    # because the original int/float-only check rejected it. Accept
    # both shapes; anything else (missing, non-numeric string) still
    # degrades to None rather than raising or guessing.
    months: float | None = None
    if isinstance(months_raw, (int, float)):
        months = float(months_raw)
    elif isinstance(months_raw, str):
        try:
            months = float(months_raw.strip())
        except ValueError:
            months = None
    if months is not None and months > 0:
        years = int(months) // 12
        return f"{years}+ years" if years >= 1 else f"{int(months)}+ months"
    return None


def _ld_skills(ld: dict | None) -> list[str] | None:
    """
    Naukri's own flat `skills` array from the schema.org JobPosting
    (evidenced from a real capture: content-identical to, and same
    order as, the visible Key Skills chip widget, just without the
    preferred/required distinction — see selectors.KEY_SKILLS_*).
    Raw evidence, preserved verbatim: no dedup, no reclassification,
    no inference of anything not literally in the array. Accepts only
    the observed shape (a list; individual non-string entries inside
    it are skipped, not fatal) — anything else degrades to None rather
    than raising or guessing at a different shape.
    """
    if not ld:
        return None
    raw = ld.get("skills")
    if not isinstance(raw, list):
        return None
    cleaned = [_clean(s) for s in raw if isinstance(s, str)]
    cleaned = [s for s in cleaned if s]
    return cleaned or None


def _key_skills_dom(page: Any) -> list[KeySkillChip] | None:
    """
    Best-effort read of Naukri's visible Key Skills chip widget
    (selectors.KEY_SKILLS_*) — UNVERIFIED, hashed-class DOM, evidenced
    from exactly one real capture. Never raises and never fails the
    job: a missing container, a selector miss, or any per-chip read
    error degrades to None (whole field) or simply skips that one chip,
    exactly like every other DOM read in this module. Returning None
    means "not observed this fetch", never "confirmed absent" — same
    convention as browser/models.py's ResumeState/ApplyUiInspection.
    """
    try:
        container = page.query_selector(selectors.KEY_SKILLS_CONTAINER)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_job_detail: key skills container query raised %s", exc)
        return None
    if container is None:
        return None
    try:
        chip_elements = container.query_selector_all(selectors.KEY_SKILLS_CHIP)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_job_detail: key skills chip query raised %s", exc)
        return None

    chips: list[KeySkillChip] = []
    for chip in chip_elements or []:
        try:
            text = (chip.inner_text() or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("fetch_job_detail: key skill chip text raised %s", exc)
            continue
        if not text:
            continue
        try:
            preferred = chip.query_selector(selectors.KEY_SKILLS_PREFERRED_ICON) is not None
        except Exception as exc:  # noqa: BLE001
            logger.debug("fetch_job_detail: key skill chip icon query raised %s", exc)
            preferred = False
        chips.append(KeySkillChip(text=text, preferred=preferred))
    return chips or None


_BLOCK_TAG_RE = _re.compile(r"</(?:p|div|li|ul|ol|h[1-6]|tr|section)>|<br\s*/?>", _re.I)
_TAG_RE = _re.compile(r"<[^>]+>")


def _html_to_text(raw: Any) -> str | None:
    """Flatten a description HTML fragment to plain text (block tags -> a
    newline, other tags dropped, entities unescaped, whitespace tidied)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = _BLOCK_TAG_RE.sub("\n", raw)
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text)
    text = _re.sub(r"[ \t]+", " ", text)
    text = _re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", text)
    return text.strip() or None


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
                posted_text=_text_or_none(card, selectors.JOB_CARD_POSTED),
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
