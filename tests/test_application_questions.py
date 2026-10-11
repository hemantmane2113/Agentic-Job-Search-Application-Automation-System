"""Phase 14: ApplicationQuestion audit log. Purely additive — never
consulted by scoring/matching/recommendation logic."""

from __future__ import annotations

import uuid

from naukri_agent.database.base import session_scope
from naukri_agent.database.models import ApplicationQuestion, ApplicationStatus
from naukri_agent.database.repositories import (
    add_application_question,
    link_application_questions_to_history,
    list_application_questions,
    upsert_application_history,
)

from .digest_fakes import add_job, in_memory_factory


def test_new_table_reachable_on_a_fresh_db():
    with session_scope(in_memory_factory()) as s:
        assert s.query(ApplicationQuestion).count() == 0


def test_add_and_list_round_trip():
    with session_scope(in_memory_factory()) as s:
        job = add_job(s, slug="x", ext="040926000100")
        attempt_id = uuid.uuid4().hex
        add_application_question(
            s, job_id=job.id, attempt_id=attempt_id, order_in_attempt=1,
            question_text="Notice period?", drafted_answer="30 days",
            final_answer="30 days", llm_provider="fake", llm_model="m",
        )
        add_application_question(
            s, job_id=job.id, attempt_id=attempt_id, order_in_attempt=2,
            question_text="Relocate?", was_skipped=True,
        )
        rows = list_application_questions(s, attempt_id=attempt_id)
        assert [r.order_in_attempt for r in rows] == [1, 2]
        assert rows[0].final_answer == "30 days"
        assert rows[1].was_skipped is True
        assert rows[1].application_id is None


def test_link_to_history_backfills_only_the_given_attempt():
    with session_scope(in_memory_factory()) as s:
        job = add_job(s, slug="x", ext="040926000101")
        attempt_a = uuid.uuid4().hex
        attempt_b = uuid.uuid4().hex
        add_application_question(
            s, job_id=job.id, attempt_id=attempt_a, order_in_attempt=1, question_text="Q1"
        )
        add_application_question(
            s, job_id=job.id, attempt_id=attempt_b, order_in_attempt=1, question_text="Q2"
        )
        app_row, _created = upsert_application_history(
            s, job.id, status=ApplicationStatus.APPLIED, source="agent_auto_apply"
        )
        updated = link_application_questions_to_history(s, attempt_a, app_row.id)
        assert updated == 1

        rows_a = list_application_questions(s, attempt_id=attempt_a)
        rows_b = list_application_questions(s, attempt_id=attempt_b)
        assert rows_a[0].application_id == app_row.id
        assert rows_b[0].application_id is None


def test_list_application_questions_by_job_id():
    with session_scope(in_memory_factory()) as s:
        job1 = add_job(s, slug="a", ext="040926000102")
        job2 = add_job(s, slug="b", ext="040926000103")
        add_application_question(
            s, job_id=job1.id, attempt_id="a1", order_in_attempt=1, question_text="Q1"
        )
        add_application_question(
            s, job_id=job2.id, attempt_id="a2", order_in_attempt=1, question_text="Q2"
        )
        assert len(list_application_questions(s, job_id=job1.id)) == 1
        assert len(list_application_questions(s)) == 2


def test_rows_survive_an_aborted_attempt_with_no_application_row():
    """An abort before final confirm should never retroactively delete
    the questions already asked — the audit trail stays intact."""
    with session_scope(in_memory_factory()) as s:
        job = add_job(s, slug="x", ext="040926000104")
        attempt_id = uuid.uuid4().hex
        add_application_question(
            s, job_id=job.id, attempt_id=attempt_id, order_in_attempt=1, question_text="Q1"
        )
        rows = list_application_questions(s, attempt_id=attempt_id)
        assert len(rows) == 1
        assert rows[0].application_id is None
