"""Tests for ``cleanup_job.cleanup_abandoned_sessions_task``.

Behaviour under test:
* Sessions older than ``SESSION_GRACE_MINUTES`` with no Job rows are
  deleted.
* Sessions younger than the grace window are NOT touched.
* Sessions with ≥1 Job row are NEVER deleted, regardless of age.
* The AuditEvent for a deleted session survives with
  ``session_id=NULL`` (per the existing ``ondelete=SET NULL`` schema).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


import uuid as _uuid


def _seed_session(db, *, age_minutes, has_job=False, ip="198.51.100.1",
                  short_suffix=""):
    from db.models import AuditEvent, Job, Session

    created = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    s = Session(
        short_code=f"cl-{_uuid.uuid4().hex[:10]}",
        edit_secret="x" * 32,
        state={},
    )
    db.add(s)
    db.flush()
    s.created_at = created.replace(tzinfo=None)
    db.flush()

    ae = AuditEvent(session_id=s.id, ip=ip, action="session_create")
    db.add(ae)
    db.flush()

    if has_job:
        j = Job(
            session_id=s.id,
            kind="find_pockets",
            status="completed",
            legacy_id=f"fp-{s.short_code}",
        )
        db.add(j)
        db.flush()
    db.commit()
    return s


@pytest.fixture
def grace_15(monkeypatch):
    monkeypatch.setattr("config.Config.SESSION_GRACE_MINUTES", 15)


class TestCleanupAbandonedSessions:
    def test_deletes_old_empty_session(self, db_with_schema, grace_15):
        from cleanup_job import cleanup_abandoned_sessions_task
        from db.models import Session
        from db.session import get_db
        from sqlalchemy import select

        with get_db() as db:
            s = _seed_session(db, age_minutes=60, has_job=False)
            sid = s.id

        result = cleanup_abandoned_sessions_task()
        assert result["status"] == "success"
        assert result["deleted"] == 1

        with get_db() as db:
            row = db.scalars(select(Session).where(Session.id == sid)).one_or_none()
        assert row is None

    def test_keeps_fresh_empty_session(self, db_with_schema, grace_15):
        """Within the 15-min grace window → leave alone."""
        from cleanup_job import cleanup_abandoned_sessions_task
        from db.models import Session
        from db.session import get_db
        from sqlalchemy import select

        with get_db() as db:
            s = _seed_session(db, age_minutes=5, has_job=False)
            sid = s.id

        result = cleanup_abandoned_sessions_task()
        assert result["deleted"] == 0

        with get_db() as db:
            row = db.scalars(select(Session).where(Session.id == sid)).one_or_none()
        assert row is not None  # still here

    def test_keeps_old_session_with_job(self, db_with_schema, grace_15):
        """Old but committed → must never be deleted."""
        from cleanup_job import cleanup_abandoned_sessions_task
        from db.models import Session
        from db.session import get_db
        from sqlalchemy import select

        with get_db() as db:
            s = _seed_session(db, age_minutes=120, has_job=True)
            sid = s.id

        result = cleanup_abandoned_sessions_task()
        assert result["deleted"] == 0

        with get_db() as db:
            row = db.scalars(select(Session).where(Session.id == sid)).one_or_none()
        assert row is not None

    def test_mixed_population(self, db_with_schema, grace_15):
        """Three categories at once — only old+empty get deleted."""
        from cleanup_job import cleanup_abandoned_sessions_task
        from db.models import Session
        from db.session import get_db
        from sqlalchemy import select

        with get_db() as db:
            # Should be deleted
            _seed_session(db, age_minutes=30, has_job=False, short_suffix="a")
            _seed_session(db, age_minutes=120, has_job=False, short_suffix="b")
            _seed_session(db, age_minutes=600, has_job=False, short_suffix="c")
            # Should survive
            _seed_session(db, age_minutes=2, has_job=False, short_suffix="d")
            _seed_session(db, age_minutes=90, has_job=True, short_suffix="e")
            _seed_session(db, age_minutes=500, has_job=True, short_suffix="f")

        result = cleanup_abandoned_sessions_task()
        assert result["deleted"] == 3

        with get_db() as db:
            n = db.scalars(select(Session)).all()
        assert len(n) == 3  # the three survivors

    def test_audit_event_survives_session_delete(self, db_with_schema, grace_15):
        """``AuditEvent.session_id`` is ``ondelete=SET NULL`` in the
        schema — the audit row must survive a session deletion (for
        forensic continuity). On Postgres the FK fires and nulls the
        session_id; SQLite (tests) doesn't enforce FK cascades by
        default, so we just assert the audit row still exists."""
        from cleanup_job import cleanup_abandoned_sessions_task
        from db.models import AuditEvent, Session
        from db.session import get_db
        from sqlalchemy import select

        with get_db() as db:
            s = _seed_session(db, age_minutes=60, has_job=False)
            sid = s.id

        cleanup_abandoned_sessions_task()

        with get_db() as db:
            sess = db.scalars(select(Session).where(Session.id == sid)).one_or_none()
            assert sess is None
            audit_rows = list(db.scalars(
                select(AuditEvent).where(AuditEvent.action == "session_create")
            ).all())
        assert len(audit_rows) == 1  # survives the cascade
