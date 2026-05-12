"""SQLAlchemy ORM models for v2 (Phase A commit A2).

Two tables:

* ``sessions`` — one row per anonymous shareable workspace. ``short_code`` +
  ``edit_secret`` live in the URL the user shares; ``state`` (JSONB) holds the
  loose "what's currently configured" payload; ``expired_at`` powers the
  soft-expiry policy locked in the strategy doc.
* ``jobs`` — one row per compute step inside a session (Find Pockets / Cluster
  / Docking). Replaces the disk-based ``<job>_status.json`` once commit A3
  pivots the task layer. For A2 the table exists but is empty.

Both tables use UUID primary keys (Postgres ``uuid`` type, SQLite ``CHAR(36)``).
JSONB columns degrade to ``JSON`` on non-Postgres dialects so the SQLite test
suite keeps working.
"""
from __future__ import annotations

import datetime as _dt
import uuid

from sqlalchemy import JSON, DateTime, ForeignKey, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from db.base import Base


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# Portable UUID: Uuid on Postgres (native), CHAR(36) on SQLite. SQLAlchemy
# 2.0's ``Uuid`` type handles the bind/result conversion in both directions.
_UUID = Uuid(as_uuid=True)

# JSONB on Postgres, JSON on SQLite.
_JSONB = JSONB().with_variant(JSON(), "sqlite")


class Session(Base):
    """Anonymous shareable workspace.

    The URL ``/s/<short_code>`` opens the session read-only;
    ``/s/<short_code>?edit=<edit_secret>`` grants mutation. The secret is part
    of the URL the creator copies and shares with collaborators — by design,
    not hashed.
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID, primary_key=True, default=uuid.uuid4
    )
    short_code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    edit_secret: Mapped[str] = mapped_column(String(48), nullable=False)

    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_active_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expired_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Auth-readiness: nullable now, populated once a login flow exists.
    user_id: Mapped[uuid.UUID | None] = mapped_column(_UUID, nullable=True)

    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Loose "what's currently configured" — selected clusters, box dims, etc.
    # Normalized first-class concepts (job rows) live in the ``jobs`` table.
    state: Mapped[dict] = mapped_column(_JSONB, nullable=False, default=dict)

    jobs: Mapped[list["Job"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return f"<Session id={self.id} short={self.short_code} expired={self.expired_at is not None}>"


class Job(Base):
    """One compute step inside a session.

    ``kind`` is the stage name (``find_pockets`` | ``cluster`` | ``docking``);
    ``status`` is the current Celery-aligned state (``submitted`` | ``running``
    | ``completed`` | ``failed`` | ``cancelled``).

    ``result_info``, ``error``, and ``pair_failures`` mirror the JSON shapes
    that ``tasks._update_status_file`` + ``_fail_job`` already write to disk
    today. A3 pivots those call sites to write here instead.
    """

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        _UUID, primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="submitted", index=True)
    step: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Celery task ID — opaque string from the broker. Useful for cross-checks
    # while Celery is still in the stack (Phases A & B). Goes away in Phase C
    # when each session has its own dedicated worker.
    celery_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    result_info: Mapped[dict | None] = mapped_column(_JSONB, nullable=True)
    error: Mapped[dict | None] = mapped_column(_JSONB, nullable=True)
    pair_failures: Mapped[list | None] = mapped_column(_JSONB, nullable=True)
    pair_failures_log: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=_now_utc,
    )

    session: Mapped[Session] = relationship(back_populates="jobs")

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return f"<Job id={self.id} kind={self.kind} status={self.status}>"
