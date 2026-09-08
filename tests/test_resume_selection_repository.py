import pytest
from sqlalchemy.exc import IntegrityError

from naukri_agent.config import Settings
from naukri_agent.database.base import init_db, session_scope
from naukri_agent.database.models import Candidate, Job, ResumeSelection
from naukri_agent.database.repositories import upsert_resume_selection
from naukri_agent.resume.registry import ResumeMatchVia, ResumeSelectionDecision
from naukri_agent.resume.selector import ResumeSelectionOutcome


def _in_memory_settings() -> Settings:
    return Settings(_env_file=None, database_url="sqlite:///:memory:")


def _job_and_candidate(session) -> tuple[int, int]:
    job = Job(
        url="https://example.com/1", title="t", company="c", location="l",
        description="d", content_fingerprint="fp",
    )
    candidate = Candidate(full_name="X", email="x@example.com", years_experience=3, profile_json="{}")
    session.add_all([job, candidate])
    session.flush()
    return job.id, candidate.id


def _selected_outcome(**overrides) -> ResumeSelectionOutcome:
    defaults = dict(
        decision=ResumeSelectionDecision.SELECTED,
        resume_id="data_scientist",
        file="resumes/data_scientist.pdf",
        file_hash="abc123",
        matched_via=ResumeMatchVia.DETERMINISTIC,
        reason="Matched by role.",
    )
    defaults.update(overrides)
    return ResumeSelectionOutcome(**defaults)


def test_upsert_creates_new_selection():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job_id, candidate_id = _job_and_candidate(session)
        selection, created = upsert_resume_selection(session, job_id, _selected_outcome(), candidate_id)
        assert created is True
        assert selection.resume_id == "data_scientist"
        assert selection.decision == ResumeSelectionDecision.SELECTED


def test_upsert_updates_existing_instead_of_duplicating():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job_id, candidate_id = _job_and_candidate(session)
        upsert_resume_selection(
            session, job_id, _selected_outcome(resume_id="data_analyst"), candidate_id
        )
    with session_scope(session_factory) as session:
        job_id = session.query(Job).one().id
        candidate_id = session.query(Candidate).one().id
        selection, created = upsert_resume_selection(
            session, job_id, _selected_outcome(resume_id="data_scientist"), candidate_id
        )
        assert created is False
        assert selection.resume_id == "data_scientist"

    with session_scope(session_factory) as session:
        assert session.query(ResumeSelection).count() == 1


def test_review_outcome_persisted_with_null_resume_id():
    session_factory = init_db(_in_memory_settings())
    outcome = ResumeSelectionOutcome(
        decision=ResumeSelectionDecision.REVIEW, reason="No matching role."
    )
    with session_scope(session_factory) as session:
        job_id, candidate_id = _job_and_candidate(session)
        selection, _ = upsert_resume_selection(session, job_id, outcome, candidate_id)
        assert selection.decision == ResumeSelectionDecision.REVIEW
        assert selection.resume_id is None


def test_duplicate_prevented_at_db_level():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job_id, candidate_id = _job_and_candidate(session)
        session.add(
            ResumeSelection(
                job_id=job_id,
                candidate_id=candidate_id,
                decision=ResumeSelectionDecision.SELECTED,
                resume_id="data_scientist",
                reason="r",
            )
        )

    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            session.add(
                ResumeSelection(
                    job_id=job_id,
                    candidate_id=candidate_id,
                    decision=ResumeSelectionDecision.SELECTED,
                    resume_id="data_analyst",
                    reason="r2",
                )
            )


def test_file_hash_and_path_persisted():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        job_id, candidate_id = _job_and_candidate(session)
        upsert_resume_selection(session, job_id, _selected_outcome(), candidate_id)

    with session_scope(session_factory) as session:
        selection = session.query(ResumeSelection).one()
        assert selection.file_hash == "abc123"
        assert selection.file_path == "resumes/data_scientist.pdf"
        assert selection.matched_via == ResumeMatchVia.DETERMINISTIC
