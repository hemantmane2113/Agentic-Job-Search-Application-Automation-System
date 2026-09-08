"""
SQLAlchemy engine/session setup.

Phase 1 establishes the pattern the rest of the project will reuse:
one shared declarative Base, an engine built from Settings.database_url,
and a session factory. Later phases add models to models.py and import
Base from here — they should never create their own engine.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from naukri_agent.config import Settings


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models in the project."""


def make_engine(settings: Settings):
    """
    Build a SQLAlchemy engine from settings.database_url.

    For SQLite specifically, ensures the parent directory of the
    database file exists (SQLite will not create it) and passes
    check_same_thread=False, since the pipeline may touch the session
    from a scheduler thread as well as the CLI.
    """
    connect_args = {}
    if settings.database_url.startswith("sqlite"):
        db_path = settings.database_url.removeprefix("sqlite:///")
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        connect_args["check_same_thread"] = False

    return create_engine(settings.database_url, connect_args=connect_args)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(settings: Settings) -> sessionmaker[Session]:
    """
    Create all tables (if they don't exist) and return a session
    factory bound to the resulting engine. Called once at startup by
    the CLI / pipeline; tests typically call this with an in-memory
    sqlite URL instead.
    """
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """
    Provide a transactional scope: commits on success, rolls back and
    re-raises on any exception, always closes the session.

    Usage:
        with session_scope(session_factory) as session:
            session.add(some_model)
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
