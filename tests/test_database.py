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


def test_a_write_waits_for_another_writer_instead_of_failing_at_once(tmp_path):
    import sqlite3
    import threading
    import time

    from sqlalchemy import text

    from naukri_agent.database.base import make_engine

    db = tmp_path / "busy.db"
    engine = make_engine(Settings(database_url=f"sqlite:///{db}", database_busy_wait_seconds=10))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (x INTEGER)"))

    other = sqlite3.connect(db, timeout=10, isolation_level=None, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")  # another process (the daily run) holds the write lock

    def release_soon():
        time.sleep(1.5)
        other.execute("COMMIT")

    threading.Thread(target=release_soon).start()
    started = time.monotonic()
    with engine.begin() as conn:  # would raise "database is locked" at once without a wait
        conn.execute(text("INSERT INTO t VALUES (1)"))
    assert time.monotonic() - started >= 1.0
    other.close()


def test_the_busy_wait_defaults_to_30_seconds():
    assert Settings().database_busy_wait_seconds == 30
