from naukri_agent.config import Settings
from naukri_agent.database.base import init_db, session_scope
from naukri_agent.database.models import DailyRun, Job, JobExtraction
from naukri_agent.database.repositories import add_job_extraction, upsert_job
from naukri_agent.jobs.models import JobCreate, JobExtractionCreate, JobType


def _in_memory_settings() -> Settings:
    return Settings(_env_file=None, database_url="sqlite:///:memory:")


def _job(**overrides) -> JobCreate:
    defaults = dict(
        title="Data Scientist",
        company="Acme Corp",
        location="Pune",
        salary_text="8-12 LPA",
        experience_text="2-4 years",
        description="Looking for a data scientist with Python and SQL.",
        url="https://www.naukri.com/job-listings-data-scientist-pune-100001",
        posted_date_text="3 days ago",
    )
    defaults.update(overrides)
    return JobCreate(**defaults)


# --- Basic creation / dedup ---


def test_upsert_job_creates_new_job():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, created = upsert_job(session, _job())
        assert created is True
        assert job.times_seen == 1
        assert job.external_id == "100001"


def test_upsert_job_same_url_does_not_duplicate():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        upsert_job(session, _job())
    with session_scope(session_factory) as session:
        job, created = upsert_job(session, _job())
        assert created is False
        assert job.times_seen == 2

    with session_scope(session_factory) as session:
        assert session.query(Job).count() == 1


def test_resighting_updates_raw_fields_but_preserves_first_seen_at():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        upsert_job(session, _job(description="Original description."))
    with session_scope(session_factory) as session:
        first_seen = session.query(Job).one().first_seen_at

    with session_scope(session_factory) as session:
        upsert_job(session, _job(description="Updated description, role expanded."))

    with session_scope(session_factory) as session:
        job = session.query(Job).one()
        assert job.description == "Updated description, role expanded."
        assert job.first_seen_at == first_seen


# --- Repost detection ---


def test_different_url_same_content_is_linked_as_repost():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        original, _ = upsert_job(
            session,
            _job(url="https://www.naukri.com/job-listings-data-scientist-pune-100001"),
        )
        original_id = original.id

    with session_scope(session_factory) as session:
        repost, created = upsert_job(
            session,
            _job(url="https://www.naukri.com/job-listings-data-scientist-pune-999999"),
        )
        assert created is True
        assert repost.repost_of_job_id == original_id

    with session_scope(session_factory) as session:
        assert session.query(Job).count() == 2


def test_genuinely_different_job_is_not_linked_as_repost():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        upsert_job(session, _job(url="https://www.naukri.com/job-listings-a-100001"))
    with session_scope(session_factory) as session:
        job, _ = upsert_job(
            session,
            _job(
                url="https://www.naukri.com/job-listings-b-200002",
                description="A completely unrelated job description.",
            ),
        )
        assert job.repost_of_job_id is None


# --- DailyRun linkage ---


def test_job_records_first_and_last_seen_run():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        run1 = DailyRun()
        session.add(run1)
        session.flush()
        job, _ = upsert_job(session, _job(), run_id=run1.id)
        assert job.first_seen_run_id == run1.id
        assert job.last_seen_run_id == run1.id

    with session_scope(session_factory) as session:
        run2 = DailyRun()
        session.add(run2)
        session.flush()
        job, created = upsert_job(session, _job(), run_id=run2.id)
        assert created is False
        assert job.last_seen_run_id == run2.id
        assert job.first_seen_run_id != run2.id


# --- Extraction audit trail ---


def test_add_job_extraction_never_touches_raw_job_row():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job(description="Original raw description."))
        job_id = job.id

    with session_scope(session_factory) as session:
        add_job_extraction(
            session,
            job_id,
            JobExtractionCreate(
                required_skills=["Python", "SQL"],
                job_type=JobType.FULL_TIME,
                raw_llm_response='{"required_skills": ["Python", "SQL"]}',
            ),
        )

    with session_scope(session_factory) as session:
        job = session.query(Job).filter_by(id=job_id).one()
        assert job.description == "Original raw description."


def test_multiple_extractions_are_versioned_not_overwritten():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        job_id = job.id

    with session_scope(session_factory) as session:
        add_job_extraction(
            session, job_id, JobExtractionCreate(normalized_title="Data Scientist I")
        )
    with session_scope(session_factory) as session:
        add_job_extraction(
            session, job_id, JobExtractionCreate(normalized_title="Data Scientist II")
        )

    with session_scope(session_factory) as session:
        extractions = (
            session.query(JobExtraction)
            .filter_by(job_id=job_id)
            .order_by(JobExtraction.extraction_version)
            .all()
        )
        assert len(extractions) == 2
        assert extractions[0].extraction_version == 1
        assert extractions[0].is_current is False
        assert extractions[1].extraction_version == 2
        assert extractions[1].is_current is True


def test_raw_llm_response_is_preserved_verbatim():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(session, _job())
        job_id = job.id

    raw = '{"required_skills": ["Python"], "note": "verbatim LLM output"}'
    with session_scope(session_factory) as session:
        add_job_extraction(session, job_id, JobExtractionCreate(raw_llm_response=raw))

    with session_scope(session_factory) as session:
        extraction = session.query(JobExtraction).filter_by(job_id=job_id).one()
        assert extraction.raw_llm_response == raw
