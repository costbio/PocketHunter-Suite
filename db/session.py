"""SQLAlchemy engine + session factory for PocketHunter v2.

The engine is process-wide and created lazily on first ``get_engine()`` call
so import-time failures (e.g., Postgres not yet reachable in a CI worker)
don't crash modules that merely import this file for type references.

``DATABASE_URL`` is read from the environment. The full pydantic-settings
``Settings`` class arrives in commit A4 and will subsume this; for A1 we
keep things minimal.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker] = None


def _resolve_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Configure it in .env "
            "(see .env.example) before starting the app."
        )
    return url


def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first call.

    Pre-ping checks keep idle connections healthy across the per-page rerun
    pattern Streamlit uses.
    """
    global _engine
    if _engine is None:
        _engine = create_engine(
            _resolve_database_url(),
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def _get_sessionmaker() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
    return _SessionLocal


@contextmanager
def get_db() -> Iterator[Session]:
    """Yield a SQLAlchemy session. Commits on success, rolls back on exception.

    Use::

        with get_db() as db:
            db.add(obj)
            # commit happens at context exit
    """
    db = _get_sessionmaker()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# Lazy proxies so import-time DATABASE_URL absence doesn't crash unrelated
# modules (some tests import db.* without ever opening a connection).
class _LazyEngine:
    def __getattr__(self, name):
        return getattr(get_engine(), name)


class _LazySessionLocal:
    def __call__(self, *args, **kwargs):
        return _get_sessionmaker()(*args, **kwargs)


engine = _LazyEngine()
SessionLocal = _LazySessionLocal()
