"""SQLAlchemy declarative base.

Kept tiny so future model modules (commit A2: ``Session``, ``Job``) import
``from db.base import Base`` without dragging in the engine module — useful
for Alembic, which imports ``Base.metadata`` for autogenerate.
"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """All ORM models inherit from this. Alembic discovers tables via
    ``Base.metadata``."""

    pass
