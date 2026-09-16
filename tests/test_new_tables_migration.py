"""Stage A: the six new tables (application_history, application_events,
job_recommendations, run_events, job_raw_skill_evidence,
job_extraction_skill_evidence) are added by init_db() to a database
that predates them, without touching existing rows.

No Alembic — Base.metadata.create_all only issues CREATE TABLE for
tables that do not yet exist.
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect

from naukri_agent.config import Settings
from naukri_agent.database.base import init_db, make_session_factory, session_scope
from naukri_agent.database.models import (
    ApplicationHistory,
    ApplicationStatus,
    Base,
    JobExtractionSkillEvidence,
    JobRawSkillEvidence,
    JobRecommendation,
    RunEvent,
)
from naukri_agent.database.repositories import (
    application_status_for_job,
    replace_raw_skill_evidence,
    upsert_application_history,
    upsert_job,
)
from naukri_agent.jobs.models import JobCreate

_NEW_TABLES = {
    "application_history",
    "application_events",
    "job_recommendations",
    "run_events",
    "job_raw_skill_evidence",
    "job_extraction_skill_evidence",
}


def _old_schema_engine(db_url: str):
    """create_all for every table EXCEPT the four Stage A additions."""
    engine = create_engine(db_url)
    old_tables = [
        t for name, t in Base.metadata.tables.items() if name not in _NEW_TABLES
    ]
    Base.metadata.create_all(engine, tables=old_tables)
    return engine


def test_init_db_adds_new_tables_and_keeps_existing_rows(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'legacy.db'}"

    # 1. a "legacy" DB: old tables only, with a real Job row in it
    engine = _old_schema_engine(db_url)
    insp = inspect(engine)
    assert not (_NEW_TABLES & set(insp.get_table_names()))
    with session_scope(make_session_factory(engine)) as s:
        job, _ = upsert_job(
            s,
            JobCreate(
                title="Legacy Role", company="OldCo", location="Pune",
                description="pre-existing JD",
                url="https://www.naukri.com/job-listings-legacy-040926000001",
            ),
        )
        legacy_job_id = job.id
    engine.dispose()

    # 2. init_db on the same file -> should CREATE the four new tables only
    settings = Settings(_env_file=None, database_url=db_url)
    factory = init_db(settings)

    insp2 = inspect(create_engine(db_url))
    assert _NEW_TABLES <= set(insp2.get_table_names())

    with session_scope(factory) as s:
        from naukri_agent.database.models import Job

        # existing row untouched
        j = s.get(Job, legacy_job_id)
        assert j is not None and j.title == "Legacy Role" and j.company == "OldCo"

        # new tables are empty but usable
        assert s.query(ApplicationHistory).count() == 0
        assert s.query(JobRecommendation).count() == 0
        assert s.query(RunEvent).count() == 0
        assert s.query(JobRawSkillEvidence).count() == 0
        assert s.query(JobExtractionSkillEvidence).count() == 0

        assert application_status_for_job(s, legacy_job_id) == ApplicationStatus.NOT_APPLIED
        row, created = upsert_application_history(
            s, legacy_job_id, status=ApplicationStatus.APPLIED
        )
        assert created is True and row.job_id == legacy_job_id
        assert application_status_for_job(s, legacy_job_id) == ApplicationStatus.APPLIED

        # the new skill-evidence table is usable against the legacy job row
        rows = replace_raw_skill_evidence(s, legacy_job_id, ld_json_skills=["Python", "SQL"])
        assert {r.skill_text for r in rows} == {"Python", "SQL"}


def test_init_db_is_idempotent_on_a_fully_migrated_db(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'current.db'}"
    settings = Settings(_env_file=None, database_url=db_url)
    init_db(settings)
    # a second call must not raise and must not drop data
    factory = init_db(settings)
    with session_scope(factory) as s:
        job, _ = upsert_job(
            s,
            JobCreate(
                title="R", company="C", location="",
                description="d",
                url="https://www.naukri.com/job-listings-x-040926000009",
            ),
        )
        upsert_application_history(s, job.id, status=ApplicationStatus.APPLIED)
    factory2 = init_db(settings)
    with session_scope(factory2) as s:
        assert s.query(ApplicationHistory).count() == 1
