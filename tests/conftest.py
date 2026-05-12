"""Shared pytest fixtures for the DB-backed tests.

Each test gets a fresh in-memory SQLite database with the full schema
created via ``Base.metadata.create_all``. JSONB/UUID columns are defined
with SQLite-compatible variants in ``db/models.py`` so the suite runs
without a real Postgres.

We set ``DATABASE_URL`` and ``BASE_URL`` at module import time so any
test module that transitively imports ``config`` / ``settings`` (e.g.
``security``, ``rate_limiter``) doesn't trip the pydantic "required"
validators at collection time.
"""
from __future__ import annotations

import os

# Set required-by-pydantic env vars BEFORE any test imports happen.
# Individual tests can monkeypatch these or instantiate Settings directly
# for negative cases.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("BASE_URL", "http://localhost:8501")

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
