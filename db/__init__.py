"""Database layer for PocketHunter Suite (Phase A scaffolding).

Public surface:
    - ``db.base.Base`` — SQLAlchemy declarative base. Tables (added in commit
      A2) subclass this so Alembic's autogenerate picks them up.
    - ``db.session.engine`` — process-wide SQLAlchemy engine.
    - ``db.session.SessionLocal`` — sessionmaker for short-lived units of work.
    - ``db.session.get_db()`` — context manager yielding a Session that
      commits on success and rolls back on exception.

Phase A is intentionally connection-only; no models, no consumers. Commits
A2+ wire actual tables and call sites.
"""
from db.base import Base
from db.session import SessionLocal, engine, get_db

__all__ = ["Base", "engine", "SessionLocal", "get_db"]
