"""Stage A: read-only job-detail extraction (browser/jobs.fetch_job_detail).

Primary source is the server-rendered schema.org JobPosting
(<script type="application/ld+json">); the hashed-class visible DOM is a
per-field fallback. Never clicks/fills/submits; degrades to None per
field; never raises.
"""

from __future__ import annotations

import json

from naukri_agent.browser import jobs as jobs_module
from naukri_agent.browser import selectors
from naukri_agent.browser.models import KeySkillChip
from naukri_agent.jobs.models import compute_content_fingerprint

from .browser_fakes import FakeElement, FakePage

URL = "https://www.naukri.com/job-listings-gen-ai-data-scientist-040926008523"

LD = selectors.JOB_DETAIL_LD_JSON


def _job_posting(
    *,
    title="Gen AI Data Scientist",
    company="Sigma Allied Services",
    cities=("Pune", "Bengaluru", "Gurugram"),
    months=24,
    salary="11-21 Lacs P.A",
    description="<p>Data Science<strong>Role &amp; responsibilities</strong></p><br /><p>Build LLM/RAG systems.</p>",
    date_posted="2026-09-08",
    skills="__unset__",  # "__unset__" = omit key; None = explicit null; else the value as-is
    wrap=None,  # None | "graph" | "array"
) -> str:
    node: dict = {"@context": "http://schema.org", "@type": "JobPosting", "title": title}
    if company is not None:
        node["hiringOrganization"] = {"@type": "Organization", "name": company}
    if cities is not None:
        node["jobLocation"] = {
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressLocality": list(cities)},
        }
    if months is not None:
        node["experienceRequirements"] = {
            "@type": "OccupationalExperienceRequirements",
            "monthsOfExperience": months,
        }
    if salary is not None:
        node["baseSalary"] = {
            "@type": "MonetaryAmount",
            "currency": "INR",
            "value": {"@type": "QuantitativeValue", "value": salary, "unitText": "P.A."},
        }
    if description is not None:
        node["description"] = description
    if date_posted is not None:
        node["datePosted"] = date_posted
    if skills != "__unset__":
        node["skills"] = skills

    if wrap == "graph":
        payload: object = {"@context": "http://schema.org", "@graph": [node]}
    elif wrap == "array":
        payload = [{"@type": "WebSite", "name": "Naukri"}, node]
    else:
        payload = node
    return json.dumps(payload)


def _page_with_ld(ld_json: str, extra: dict | None = None) -> FakePage:
    page = FakePage()
    page.set_element(LD, FakeElement(text=ld_json))
    for sel, text in (extra or {}).items():
        page.set_element(sel, FakeElement(text=text))
    return page


# --- primary path: ld+json -------------------------------------------------


def test_ld_json_is_primary_source_and_never_clicks_or_fills():
    page = _page_with_ld(_job_posting())
    detail = jobs_module.fetch_job_detail(page, URL)

    assert detail.url == URL
    assert detail.title == "Gen AI Data Scientist"
    assert detail.company == "Sigma Allied Services"
    assert detail.location == "Pune, Bengaluru, Gurugram"
    assert detail.salary_text == "11-21 Lacs P.A"
    assert detail.experience_text == "2+ years"  # from monthsOfExperience: 24
    assert detail.posted_date_text == "2026-09-08"
    assert "Role & responsibilities" in detail.description
    assert "Build LLM/RAG systems." in detail.description
    assert "<" not in detail.description and "&amp;" not in detail.description

    assert page.clicked == []
    assert page.filled == {}
    assert page.goto_calls == [URL]


def test_ld_json_graph_and_array_shapes_resolve():
    for wrap in ("graph", "array"):
        page = _page_with_ld(_job_posting(wrap=wrap))
        detail = jobs_module.fetch_job_detail(page, URL)
        assert detail.title == "Gen AI Data Scientist", wrap
        assert detail.company == "Sigma Allied Services", wrap


def test_experience_prefers_visible_range_over_ld_json_months():
    page = _page_with_ld(
        _job_posting(months=24),
        extra={selectors.JOB_DETAIL_EXPERIENCE: "2 - 7 years"},
    )
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.experience_text == "2 - 7 years"


# --- fallback path: visible DOM -----------------------------------------


def test_ld_json_missing_falls_back_to_css():
    page = FakePage()
    page.set_element(selectors.JOB_DETAIL_TITLE, FakeElement(text="Senior Data Scientist"))
    page.set_element(selectors.JOB_DETAIL_COMPANY, FakeElement(text="NICE Actimize"))
    page.set_element(selectors.JOB_DETAIL_LOCATION, FakeElement(text="Pune"))
    page.set_element(selectors.JOB_DETAIL_SALARY, FakeElement(text="Not disclosed"))
    page.set_element(selectors.JOB_DETAIL_EXPERIENCE, FakeElement(text="4 - 8 years"))
    page.set_element(selectors.JOB_DETAIL_STATS, FakeElement(text="Posted: 3 days ago"))
    page.set_element(selectors.JOB_DETAIL_DESCRIPTION, FakeElement(text="Own the ML platform."))

    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.title == "Senior Data Scientist"
    assert detail.company == "NICE Actimize"
    assert detail.location == "Pune"
    assert detail.salary_text == "Not disclosed"
    assert detail.experience_text == "4 - 8 years"
    assert detail.posted_date_text == "Posted: 3 days ago"
    assert detail.description == "Own the ML platform."
    assert page.clicked == []


def test_partial_ld_json_merges_with_css_per_field():
    # ld+json has identity + description but no salary / no experience.
    page = _page_with_ld(
        _job_posting(salary=None, months=None),
        extra={
            selectors.JOB_DETAIL_SALARY: "18-25 Lacs P.A.",
            selectors.JOB_DETAIL_EXPERIENCE: "5 - 9 years",
        },
    )
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.title == "Gen AI Data Scientist"       # ld+json
    assert detail.company == "Sigma Allied Services"     # ld+json
    assert detail.salary_text == "18-25 Lacs P.A."       # css fallback
    assert detail.experience_text == "5 - 9 years"       # css fallback


def test_malformed_ld_json_does_not_raise_and_falls_back():
    page = FakePage()
    page.set_element(
        LD,
        [
            FakeElement(text="{ this is not json"),
            FakeElement(text=json.dumps({"@type": "WebSite", "name": "Naukri"})),
        ],
    )
    page.set_element(selectors.JOB_DETAIL_TITLE, FakeElement(text="Data Scientist"))

    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.title == "Data Scientist"
    assert detail.company is None and detail.salary_text is None
    assert page.clicked == []


def test_missing_everything_degrades_to_none_never_raises():
    page = FakePage()  # nothing set; wait_for_selector will time out (swallowed)
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.url == URL
    assert detail.title is None and detail.description is None and detail.company is None
    assert detail.location is None and detail.salary_text is None
    assert detail.experience_text is None and detail.posted_date_text is None
    assert page.clicked == [] and page.filled == {}


def test_goto_failure_returns_bare_detail_not_exception():
    page = FakePage()

    def boom(url):
        raise RuntimeError("nav failed")

    page.goto = boom
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.url == URL and detail.title is None


# --- the run-1 regression: different pages -> different fingerprints ----


def _fp(detail) -> str:
    return compute_content_fingerprint(
        detail.title or "", detail.company or "", detail.description or ""
    )


class _RaisingElement:
    """A minimal stand-in for a Playwright element/page whose DOM calls
    raise — used to prove Key Skills extraction degrades independently
    without a shared error-injection field on FakePage/FakeElement."""

    def query_selector(self, selector: str):
        raise RuntimeError("boom")

    def query_selector_all(self, selector: str):
        raise RuntimeError("boom")


# --- Source 1: ld+json `skills` --------------------------------------------


def test_ld_json_skills_extracted_verbatim():
    page = _page_with_ld(_job_posting(skills=["Python", "AWS", "Azure", "Databricks"]))
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.ld_json_skills == ["Python", "AWS", "Azure", "Databricks"]


def test_ld_json_skills_missing_key_degrades_to_none():
    page = _page_with_ld(_job_posting(skills="__unset__"))
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.ld_json_skills is None


def test_ld_json_skills_wrong_shape_degrades_to_none_not_raise():
    # A string instead of a list -- not the observed real shape.
    page = _page_with_ld(_job_posting(skills="Python, AWS, Azure"))
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.ld_json_skills is None
    assert detail.title == "Gen AI Data Scientist"  # rest of the fetch is unaffected


def test_ld_json_skills_filters_non_string_entries_not_fatal():
    page = _page_with_ld(_job_posting(skills=["Python", 42, None, "  ", "SQL"]))
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.ld_json_skills == ["Python", "SQL"]


def test_malformed_ld_json_degrades_skills_to_none_but_dom_title_still_works():
    page = FakePage()
    page.set_element(LD, FakeElement(text="{ not json"))
    page.set_element(selectors.JOB_DETAIL_TITLE, FakeElement(text="Data Scientist"))
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.ld_json_skills is None
    assert detail.title == "Data Scientist"


# --- Source 2: Key Skills DOM widget ----------------------------------------


def _chip(text: str, preferred: bool) -> FakeElement:
    children = {selectors.KEY_SKILLS_PREFERRED_ICON: FakeElement()} if preferred else {}
    return FakeElement(text=text, children=children)


def test_key_skills_dom_extracted_with_preferred_flag():
    container = FakeElement(
        children={
            selectors.KEY_SKILLS_CHIP: [
                _chip("Python", preferred=True),
                _chip("AWS", preferred=False),
                _chip("Azure", preferred=False),
            ]
        }
    )
    page = _page_with_ld(_job_posting())
    page.set_element(selectors.KEY_SKILLS_CONTAINER, container)
    detail = jobs_module.fetch_job_detail(page, URL)

    assert detail.key_skills_dom is not None
    by_text = {c.text: c.preferred for c in detail.key_skills_dom}
    assert by_text == {"Python": True, "AWS": False, "Azure": False}


def test_key_skills_dom_container_absent_degrades_to_none():
    page = _page_with_ld(_job_posting())  # no KEY_SKILLS_CONTAINER set
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.key_skills_dom is None


def test_key_skills_dom_empty_chips_are_skipped():
    container = FakeElement(
        children={
            selectors.KEY_SKILLS_CHIP: [
                _chip("", preferred=True),
                _chip("   ", preferred=False),
                _chip("SQL", preferred=False),
            ]
        }
    )
    page = _page_with_ld(_job_posting())
    page.set_element(selectors.KEY_SKILLS_CONTAINER, container)
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.key_skills_dom == [KeySkillChip(text="SQL", preferred=False)]


def test_key_skills_dom_container_query_failure_degrades_to_none_never_raises():
    # The "container" itself raises when queried for chips -- proves
    # the per-chip query failure path degrades to None rather than
    # propagating.
    page = _page_with_ld(_job_posting())
    page.set_element(selectors.KEY_SKILLS_CONTAINER, _RaisingElement())
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.key_skills_dom is None
    assert detail.title == "Gen AI Data Scientist"  # rest of fetch unaffected


def test_key_skills_dom_failure_does_not_affect_ld_json_skills():
    """DOM enrichment failing must never take the raw ld+json skills
    list down with it -- the two sources degrade fully independently."""
    page = _page_with_ld(_job_posting(skills=["Python", "SQL"]))
    page.set_element(selectors.KEY_SKILLS_CONTAINER, _RaisingElement())
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.key_skills_dom is None
    assert detail.ld_json_skills == ["Python", "SQL"]


# --- monthsOfExperience regression (string vs numeric shape) ---------------


def test_months_of_experience_as_string_regression():
    """Real captured Naukri output (inspection_output/20260909_023839/
    04_job_listing.html) emits monthsOfExperience as the STRING "24",
    not a number -- the original int/float-only check silently
    returned None for every real job. Must resolve the same as the
    already-covered int shape."""
    page = _page_with_ld(_job_posting(months="24"))
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.experience_text == "2+ years"


def test_months_of_experience_non_numeric_string_degrades_to_none():
    page = _page_with_ld(_job_posting(months="unknown"))
    detail = jobs_module.fetch_job_detail(page, URL)
    assert detail.experience_text is None


def test_different_job_pages_produce_different_fingerprints():
    page_a = _page_with_ld(
        _job_posting(title="Gen AI Data Scientist", company="Sigma Allied Services",
                     description="<p>Build LLM/RAG systems.</p>")
    )
    page_b = _page_with_ld(
        _job_posting(title="Senior Data Scientist", company="NICE Actimize",
                     description="<p>Own the fraud-detection models.</p>")
    )
    page_c = _page_with_ld(  # identical content to A
        _job_posting(title="Gen AI Data Scientist", company="Sigma Allied Services",
                     description="<p>Build LLM/RAG systems.</p>")
    )

    a = jobs_module.fetch_job_detail(page_a, URL + "-a")
    b = jobs_module.fetch_job_detail(page_b, URL + "-b")
    c = jobs_module.fetch_job_detail(page_c, URL + "-c")

    assert _fp(a) != _fp(b)          # real distinct jobs -> distinct fingerprints
    assert _fp(a) == _fp(c)          # same content -> same fingerprint (true repost)
    assert a.title and a.company and a.description
