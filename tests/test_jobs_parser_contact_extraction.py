"""Phase 15: JD contact-email extraction (pure extraction, never a
decision — see jobs/parser.py's _normalize_contact_email_in_payload for
the deterministic, Python-side final arbiter)."""

import json

from naukri_agent.database.base import session_scope
from naukri_agent.database.models import Job
from naukri_agent.database.repositories import add_job_extraction, current_job_extraction
from naukri_agent.jobs.models import EmailApplicationSignal
from naukri_agent.jobs.parser import JobParser, _normalize_contact_email_in_payload

from .digest_fakes import add_job, in_memory_factory
from .fakes import FakeProvider


def _job(description: str = "We are hiring a Python developer.") -> Job:
    return Job(
        url="https://example.com/1", title="Software Engineer", company="Acme",
        location="Pune", description=description, content_fingerprint="fp",
    )


def _response(**overrides) -> str:
    base = {
        "normalized_title": "Software Engineer", "required_skills": [], "preferred_skills": [],
        "experience_min": None, "experience_max": None, "salary_min": None, "salary_max": None,
        "salary_currency": None, "education_requirements": [], "job_type": "unknown",
        "contact_email": None, "email_application_signal": "none",
    }
    base.update(overrides)
    return json.dumps(base)


def test_valid_apply_via_email_round_trips_unchanged():
    provider = FakeProvider(
        model="m", response=_response(contact_email="hr@acme.com", email_application_signal="apply_via_email")
    )
    result = JobParser(provider).parse(_job())
    assert result.success is True
    assert result.extraction.contact_email == "hr@acme.com"
    assert result.extraction.email_application_signal == EmailApplicationSignal.APPLY_VIA_EMAIL
    assert result.normalizations == []


def test_valid_contact_only_round_trips_unchanged():
    provider = FakeProvider(
        model="m", response=_response(contact_email="queries@acme.com", email_application_signal="contact_only")
    )
    result = JobParser(provider).parse(_job())
    assert result.extraction.email_application_signal == EmailApplicationSignal.CONTACT_ONLY


def test_signal_with_no_email_is_downgraded_to_none():
    provider = FakeProvider(
        model="m", response=_response(contact_email=None, email_application_signal="apply_via_email")
    )
    result = JobParser(provider).parse(_job())
    assert result.success is True
    assert result.extraction.email_application_signal == EmailApplicationSignal.NONE
    assert any("email_application_signal" in n for n in result.normalizations)


def test_invalid_email_syntax_is_dropped():
    provider = FakeProvider(
        model="m", response=_response(contact_email="not-an-email", email_application_signal="apply_via_email")
    )
    result = JobParser(provider).parse(_job())
    assert result.success is True
    assert result.extraction.contact_email is None
    assert result.extraction.email_application_signal == EmailApplicationSignal.NONE
    assert any("contact_email" in n for n in result.normalizations)
    assert any("email_application_signal" in n for n in result.normalizations)


def test_default_no_email_mentioned():
    provider = FakeProvider(model="m", response=_response())
    result = JobParser(provider).parse(_job())
    assert result.extraction.contact_email is None
    assert result.extraction.email_application_signal == EmailApplicationSignal.NONE


def test_raw_llm_response_never_rewritten_by_coercion():
    raw = _response(contact_email="not-an-email", email_application_signal="apply_via_email")
    provider = FakeProvider(model="m", response=raw)
    result = JobParser(provider).parse(_job())
    assert result.raw_response == raw
    assert result.extraction.raw_llm_response == raw


def test_normalize_contact_email_helper_is_a_noop_on_clean_data():
    data = {"contact_email": "a@b.com", "email_application_signal": "apply_via_email"}
    notes = _normalize_contact_email_in_payload(data)
    assert notes == []
    assert data == {"contact_email": "a@b.com", "email_application_signal": "apply_via_email"}


def test_add_job_extraction_persists_new_columns_and_still_versions():
    with session_scope(in_memory_factory()) as s:
        job = add_job(s, slug="x", ext="040926000300")
        provider = FakeProvider(
            model="m", response=_response(contact_email="hr@acme.com", email_application_signal="apply_via_email")
        )
        result = JobParser(provider).parse(
            Job(url=job.url, title=job.title, company=job.company, location=job.location,
                description=job.description, content_fingerprint="fp")
        )
        add_job_extraction(s, job.id, result.extraction)
        current = current_job_extraction(s, job.id)
        assert current.contact_email == "hr@acme.com"
        assert current.email_application_signal == EmailApplicationSignal.APPLY_VIA_EMAIL
        assert current.extraction_version == 1

        # Re-extraction versions correctly, same as every other field.
        provider2 = FakeProvider(model="m", response=_response())
        result2 = JobParser(provider2).parse(
            Job(url=job.url, title=job.title, company=job.company, location=job.location,
                description=job.description, content_fingerprint="fp")
        )
        add_job_extraction(s, job.id, result2.extraction)
        current2 = current_job_extraction(s, job.id)
        assert current2.extraction_version == 2
        assert current2.contact_email is None


def test_add_job_extraction_persists_source_content_fingerprint():
    """orchestration/pipeline.py relies on this to skip re-parsing an
    unchanged job -- the fingerprint must round-trip exactly, and stay
    None when the caller doesn't pass one (e.g. jobs/parser.py's own
    parse_job_and_store, which predates this efficiency feature)."""
    with session_scope(in_memory_factory()) as s:
        job = add_job(s, slug="x", ext="040926000305")
        provider = FakeProvider(model="m", response=_response())
        result = JobParser(provider).parse(
            Job(url=job.url, title=job.title, company=job.company, location=job.location,
                description=job.description, content_fingerprint="fp")
        )

        row = add_job_extraction(s, job.id, result.extraction, source_content_fingerprint="cfp-123")
        assert row.source_content_fingerprint == "cfp-123"

        row_default = add_job_extraction(s, job.id, result.extraction)
        assert row_default.source_content_fingerprint is None
