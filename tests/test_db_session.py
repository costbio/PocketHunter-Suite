"""Smoke test for the v2 DB scaffold.

Phase A commit A1 is connection-only — no models, no consumers. This test
just confirms the engine factory works against an ephemeral SQLite URL so
``pytest tests/`` can run without a live Postgres in CI.

A2 will add proper model tests once the Session/Job tables land.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text


@pytest.fixture
def sqlite_database_url(monkeypatch):
    """Point DATABASE_URL at an in-memory SQLite DB for the duration of the test."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    # Reset module-level singletons so they pick up the new URL.
    import db.session as session_mod
    session_mod._engine = None
    session_mod._SessionLocal = None
    yield
    session_mod._engine = None
    session_mod._SessionLocal = None


def test_engine_factory_uses_database_url(sqlite_database_url):
    """get_engine() returns a usable Engine when DATABASE_URL is set."""
    from db.session import get_engine

    engine = get_engine()
    assert engine is not None

    # Round-trip a trivial query to confirm the connection is live.
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 42")).scalar()
    assert result == 42


def test_get_db_context_manager_commits_and_closes(sqlite_database_url):
    """get_db() yields a Session that commits on success and closes after."""
    from db.session import get_db

    with get_db() as db:
        # No models yet; just confirm the session is alive and usable.
        result = db.execute(text("SELECT 1")).scalar()
    assert result == 1


def test_get_db_rolls_back_on_exception(sqlite_database_url):
    """get_db() rolls back when the caller raises."""
    from db.session import get_db

    with pytest.raises(RuntimeError, match="boom"):
        with get_db() as db:
            db.execute(text("SELECT 1"))
            raise RuntimeError("boom")
    # No state to inspect (no tables), but the context manager must not crash
    # on its own cleanup path.


def test_missing_database_url_raises_at_resolve():
    """_resolve_database_url() refuses to fall back silently."""
    from db.session import _resolve_database_url

    # Ensure no DATABASE_URL is set
    saved = os.environ.pop("DATABASE_URL", None)
    try:
        with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
            _resolve_database_url()
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved
