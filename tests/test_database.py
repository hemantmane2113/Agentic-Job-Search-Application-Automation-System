from naukri_agent.config import Settings
from naukri_agent.database.base import init_db, session_scope
from naukri_agent.database.models import DailyRun, DailyRunStatus


def _in_memory_settings() -> Settings:
    return Settings(_env_file=None, database_url="sqlite:///:memory:")


def test_init_db_creates_tables():
    session_factory = init_db(_in_memory_settings())
    with session_scope(session_factory) as session:
        assert session.query(DailyRun).count() == 0


def test_daily_run_crud_roundtrip():
    session_factory = init_db(_in_memory_settings())

    with session_scope(session_factory) as session:
        run = DailyRun(jobs_discovered=84, jobs_evaluated=73, applications_submitted=6)
        session.add(run)

    with session_scope(session_factory) as session:
        stored = session.query(DailyRun).one()
        assert stored.jobs_discovered == 84
        assert stored.status == DailyRunStatus.STARTED


def test_rollback_on_exception_leaves_no_partial_row():
    session_factory = init_db(_in_memory_settings())

    class _Boom(Exception):
        pass

    try:
        with session_scope(session_factory) as session:
            session.add(DailyRun(jobs_discovered=1))
            raise _Boom("simulated failure mid-transaction")
    except _Boom:
        pass

    with session_scope(session_factory) as session:
        assert session.query(DailyRun).count() == 0
