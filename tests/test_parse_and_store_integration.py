from naukri_agent.config import Settings
from naukri_agent.database.base import init_db, session_scope
from naukri_agent.database.models import Job, JobExtraction
from naukri_agent.database.repositories import upsert_job
from naukri_agent.jobs.models import JobCreate
from naukri_agent.jobs.parser import parse_job_and_store
from naukri_agent.llm.exceptions import LLMRequestError

from .fakes import FailingProvider, FakeProvider
from .test_job_parser import VALID_RESPONSE


def _in_memory_settings() -> Settings:
    return Settings(_env_file=None, database_url="sqlite:///:memory:")


def test_successful_parse_persists_extraction():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(
            session,
            JobCreate(
                title="Software Engineer",
                company="Acme",
                location="Pune",
                description="Python role",
                url="https://example.com/1",
            ),
        )
        job_id = job.id
        provider = FakeProvider(model="m", response=VALID_RESPONSE)
        result = parse_job_and_store(session, job, provider)
        assert result.success is True

    with session_scope(session_factory) as session:
        extraction = session.query(JobExtraction).filter_by(job_id=job_id).one()
        assert extraction.required_skills == ["Python", "SQL"]
        assert extraction.is_current is True
        assert extraction.llm_provider == "fake"


def test_failed_parse_does_not_persist_anything():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(
            session,
            JobCreate(
                title="Software Engineer",
                company="Acme",
                location="Pune",
                description="Python role",
                url="https://example.com/1",
            ),
        )
        job_id = job.id
        provider = FailingProvider(model="m", error=LLMRequestError("boom"))
        result = parse_job_and_store(session, job, provider)
        assert result.success is False

    with session_scope(session_factory) as session:
        assert session.query(JobExtraction).filter_by(job_id=job_id).count() == 0
        # the raw Job row is completely untouched by the failed parse
        assert session.query(Job).filter_by(id=job_id).one().description == "Python role"


def test_malformed_response_does_not_persist_anything():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job, _ = upsert_job(
            session,
            JobCreate(
                title="Software Engineer",
                company="Acme",
                location="Pune",
                description="Python role",
                url="https://example.com/1",
            ),
        )
        job_id = job.id
        provider = FakeProvider(model="m", response="not valid json")
        result = parse_job_and_store(session, job, provider)
        assert result.success is False

    with session_scope(session_factory) as session:
        assert session.query(JobExtraction).filter_by(job_id=job_id).count() == 0
