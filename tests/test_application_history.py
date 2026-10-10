"""Stage A: ApplicationHistory / ApplicationEvent repository behaviour.

The DB is the ONLY authority for application status; no LLM is involved.
Identity uses external id / URL / repost relationship — never fuzzy
title/company.
"""

from __future__ import annotations

import datetime

from naukri_agent.database.base import session_scope
from naukri_agent.database.models import (
    ApplicationEvent,
    ApplicationHistory,
    ApplicationStatus,
)
from naukri_agent.database.repositories import (
    application_status_for_job,
    canonical_job_id,
    excluded_job_ids_by_status,
    get_application_history,
    resolve_canonical_job,
    set_application_status,
    upsert_application_history,
)

from .digest_fakes import add_job, in_memory_factory, make_candidate


def test_absence_of_row_is_not_applied():
    with session_scope(in_memory_factory()) as s:
        j = add_job(s, slug="x", ext="040926000001")
        assert application_status_for_job(s, j.id) == ApplicationStatus.NOT_APPLIED
        assert get_application_history(s, j.id) is None


def test_mark_applied_is_idempotent_and_records_events():
    with session_scope(in_memory_factory()) as s:
        j = add_job(s, slug="x", ext="040926000002")
        row1, created1 = upsert_application_history(s, j.id, resume_id="data_scientist")
        row2, created2 = upsert_application_history(s, j.id, status=ApplicationStatus.INTERVIEW)

        assert created1 is True and created2 is False
        assert row1.id == row2.id
        assert s.query(ApplicationHistory).count() == 1  # never a duplicate
        assert row2.status == ApplicationStatus.INTERVIEW
        assert row2.applied_at is not None  # set on the first APPLIED
        events = s.query(ApplicationEvent).order_by(ApplicationEvent.id).all()
        assert [(e.from_status, e.to_status) for e in events] == [
            (None, ApplicationStatus.APPLIED),
            (ApplicationStatus.APPLIED, ApplicationStatus.INTERVIEW),
        ]


def test_resolve_canonical_job_by_id_external_id_and_url_never_fuzzy():
    with session_scope(in_memory_factory()) as s:
        j = add_job(s, slug="gen-ai-ds", ext="040926008523", title="Gen AI DS", company="Acme")
        assert resolve_canonical_job(s, j.id).id == j.id
        assert resolve_canonical_job(s, "040926008523").id == j.id
        assert resolve_canonical_job(s, j.url).id == j.id
        # a made-up title/company string does NOT resolve
        assert resolve_canonical_job(s, "Gen AI DS at Acme") is None
        assert resolve_canonical_job(s, "999999999999") is None


def test_mark_applied_on_a_repost_attaches_to_canonical_root():
    with session_scope(in_memory_factory()) as s:
        root = add_job(s, slug="pos", ext="040926000010", description="Same JD.")
        repost = add_job(s, slug="pos-2", ext="040926000011", description="Same JD.")
        assert repost.repost_of_job_id == root.id
        assert canonical_job_id(s, repost.id) == root.id

        row, created = upsert_application_history(s, repost.id, status=ApplicationStatus.APPLIED)
        assert created is True
        assert row.job_id == root.id  # attached to canonical
        # re-marking via the root updates the same row
        row2, created2 = upsert_application_history(s, root.id, status=ApplicationStatus.OFFER)
        assert created2 is False and row2.id == row.id
        assert s.query(ApplicationHistory).count() == 1
        assert application_status_for_job(s, repost.id) == ApplicationStatus.OFFER


def test_set_application_status_creates_or_transitions():
    with session_scope(in_memory_factory()) as s:
        j = add_job(s, slug="x", ext="040926000020")
        r = set_application_status(s, j.id, ApplicationStatus.WITHDRAWN)
        assert r.status == ApplicationStatus.WITHDRAWN
        assert s.query(ApplicationEvent).count() == 1


def test_excluded_job_ids_by_status():
    with session_scope(in_memory_factory()) as s:
        a = add_job(s, slug="a", ext="040926000030")
        b = add_job(s, slug="b", ext="040926000031")
        c = add_job(s, slug="c", ext="040926000032")
        upsert_application_history(s, a.id, status=ApplicationStatus.APPLIED)
        upsert_application_history(s, b.id, status=ApplicationStatus.REJECTED)
        set_application_status(s, c.id, ApplicationStatus.NOT_APPLIED)

        excl = excluded_job_ids_by_status(s, ["APPLIED", "INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN"])
        assert excl == {a.id, b.id}
        assert excluded_job_ids_by_status(s, ["APPLIED"]) == {a.id}
        assert excluded_job_ids_by_status(s, []) == set()


def test_snapshots_survive_job_row_edits():
    with session_scope(in_memory_factory()) as s:
        j = add_job(s, slug="x", ext="040926000040", title="Original", company="OrigCo")
        upsert_application_history(s, j.id, status=ApplicationStatus.APPLIED)
        j.title = "Retitled by a re-scrape"
        j.company = "Renamed"
        s.flush()
        row = get_application_history(s, j.id)
        assert row.job_title == "Original" and row.company == "OrigCo"
        assert row.job_url == j.url and row.external_job_id == "040926000040"
