import json

from naukri_agent.candidate.models import CandidateProfile
from naukri_agent.config import Settings
from naukri_agent.database.base import init_db, session_scope
from naukri_agent.database.models import Candidate
from naukri_agent.database.repositories import upsert_candidate


def _in_memory_settings() -> Settings:
    return Settings(_env_file=None, database_url="sqlite:///:memory:")


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(full_name="Test User", email="test@example.com", phone="123")
    defaults.update(overrides)
    return CandidateProfile(**defaults)


def test_upsert_creates_new_candidate():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        upsert_candidate(session, _profile(years_experience=2))

    with session_scope(session_factory) as session:
        candidates = session.query(Candidate).all()
        assert len(candidates) == 1
        assert candidates[0].email == "test@example.com"
        assert candidates[0].years_experience == 2


def test_upsert_updates_existing_candidate_by_email_not_duplicate():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        upsert_candidate(session, _profile(years_experience=2))
    with session_scope(session_factory) as session:
        upsert_candidate(session, _profile(years_experience=3, full_name="Updated Name"))

    with session_scope(session_factory) as session:
        candidates = session.query(Candidate).all()
        assert len(candidates) == 1  # still one row, not two
        assert candidates[0].years_experience == 3
        assert candidates[0].full_name == "Updated Name"


def test_profile_json_round_trips():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        upsert_candidate(session, _profile(skills=["Python", "SQL"]))

    with session_scope(session_factory) as session:
        candidate = session.query(Candidate).one()
        stored = json.loads(candidate.profile_json)
        assert stored["skills"] == ["Python", "SQL"]
