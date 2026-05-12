"""Shared pytest fixtures for the DB-backed tests.

Each test gets a fresh in-memory SQLite database with the full schema
created via ``Base.metadata.create_all``. JSONB/UUID columns are defined
with SQLite-compatible variants in ``db/models.py`` so the suite runs
without a real Postgres.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def db_url(monkeypatch):
    """Point DATABASE_URL at a fresh in-memory SQLite, reset the lazy
    engine + sessionmaker, and tear down at the end of the test."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")

    import db.session as session_mod
    session_mod._engine = None
    session_mod._SessionLocal = None

    yield "sqlite:///:memory:"

    session_mod._engine = None
    session_mod._SessionLocal = None


@pytest.fixture
def db_with_schema(db_url):
    """Boot a fresh DB and apply the schema via ``Base.metadata.create_all``.

    Faster than running Alembic for unit tests; Alembic is exercised by the
    A1 ``alembic upgrade head`` smoke at the compose level.
    """
    from db.base import Base
    from db.session import get_engine
    from db import models  # noqa: F401 — registers tables on Base.metadata

    engine = get_engine()
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
