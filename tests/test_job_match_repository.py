import pytest
from sqlalchemy.exc import IntegrityError

from naukri_agent.config import Settings
from naukri_agent.database.base import init_db, session_scope
from naukri_agent.database.models import Candidate, Job, JobMatch
from naukri_agent.database.repositories import upsert_job_match
from naukri_agent.matching.models import CategoryScore, MatchDecision, MatchResult


def _in_memory_settings() -> Settings:
    return Settings(_env_file=None, database_url="sqlite:///:memory:")


def _candidate_and_job(session) -> tuple[int, int]:
    candidate = Candidate(
        full_name="X", email="x@example.com", years_experience=3, profile_json="{}"
    )
    job = Job(
        url="https://example.com/1",
        title="Data Scientist",
        company="Acme",
        location="Pune",
        description="d",
        content_fingerprint="fp",
    )
    session.add_all([candidate, job])
    session.flush()
    return candidate.id, job.id


def _result(score=85.0, decision=MatchDecision.ACCEPT) -> MatchResult:
    return MatchResult(
        overall_score=score,
        decision=decision,
        category_scores={
            "skills": CategoryScore(points=30, max_points=35, positive_factors=["Python matched"]),
        },
        positive_factors=["Python matched"],
        negative_factors=[],
    )


def test_upsert_creates_new_job_match():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        candidate_id, job_id = _candidate_and_job(session)
        match, created = upsert_job_match(session, candidate_id, job_id, _result())
        assert created is True
        assert match.overall_score == 85.0
        assert match.decision == MatchDecision.ACCEPT


def test_upsert_updates_existing_match_instead_of_duplicating():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        candidate_id, job_id = _candidate_and_job(session)
        upsert_job_match(session, candidate_id, job_id, _result(score=60.0, decision=MatchDecision.REJECT))

    with session_scope(session_factory) as session:
        candidate_id = session.query(Candidate).one().id
        job_id = session.query(Job).one().id
        match, created = upsert_job_match(
            session, candidate_id, job_id, _result(score=90.0, decision=MatchDecision.ACCEPT)
        )
        assert created is False
        assert match.overall_score == 90.0

    with session_scope(session_factory) as session:
        assert session.query(JobMatch).count() == 1
        assert session.query(JobMatch).one().overall_score == 90.0


def test_duplicate_match_prevented_at_db_level_even_bypassing_repository():
    """
    Belt-and-suspenders: even if something inserts a JobMatch row
    directly instead of going through upsert_job_match, the unique
    constraint on (candidate_id, job_id) makes a true duplicate
    impossible.
    """
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        candidate_id, job_id = _candidate_and_job(session)
        session.add(
            JobMatch(
                candidate_id=candidate_id,
                job_id=job_id,
                overall_score=50,
                decision=MatchDecision.REJECT,
                category_scores={},
                positive_factors=[],
                negative_factors=[],
            )
        )

    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            session.add(
                JobMatch(
                    candidate_id=candidate_id,
                    job_id=job_id,
                    overall_score=99,
                    decision=MatchDecision.ACCEPT,
                    category_scores={},
                    positive_factors=[],
                    negative_factors=[],
                )
            )


def test_job_match_stores_category_scores_and_factors():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        candidate_id, job_id = _candidate_and_job(session)
        upsert_job_match(session, candidate_id, job_id, _result())

    with session_scope(session_factory) as session:
        match = session.query(JobMatch).one()
        assert "skills" in match.category_scores
        assert match.category_scores["skills"]["points"] == 30
        assert match.positive_factors == ["Python matched"]


def test_candidate_can_have_matches_for_multiple_jobs():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        candidate = Candidate(
            full_name="X", email="x@example.com", years_experience=3, profile_json="{}"
        )
        job1 = Job(
            url="https://example.com/1", title="A", company="C1", location="Pune",
            description="d", content_fingerprint="fp1",
        )
        job2 = Job(
            url="https://example.com/2", title="B", company="C2", location="Pune",
            description="d", content_fingerprint="fp2",
        )
        session.add_all([candidate, job1, job2])
        session.flush()
        upsert_job_match(session, candidate.id, job1.id, _result())
        upsert_job_match(session, candidate.id, job2.id, _result())

    with session_scope(session_factory) as session:
        assert session.query(JobMatch).count() == 2
