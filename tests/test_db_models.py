"""Tests for the v2 ORM models."""
from __future__ import annotations

import uuid

import pytest


def test_session_round_trip(db_with_schema):
    """Insert a Session, fetch it back, confirm defaults are sensible."""
    from db.models import Session as SessionRow
    from db.session import get_db

    with get_db() as db:
        row = SessionRow(short_code="abc123", edit_secret="secret", state={"foo": 1})
        db.add(row)
        db.flush()
        rid = row.id

    with get_db() as db:
        loaded = db.get(SessionRow, rid)
        assert loaded is not None
        assert loaded.short_code == "abc123"
        assert loaded.edit_secret == "secret"
        assert loaded.state == {"foo": 1}
        assert loaded.expired_at is None
        assert loaded.created_at is not None
        assert loaded.last_active_at is not None


def test_session_unique_short_code(db_with_schema):
    """short_code is unique-indexed; a duplicate raises."""
    from sqlalchemy.exc import IntegrityError

    from db.models import Session as SessionRow
    from db.session import get_db

    with get_db() as db:
        db.add(SessionRow(short_code="dup", edit_secret="s1"))

    with pytest.raises(IntegrityError):
        with get_db() as db:
            db.add(SessionRow(short_code="dup", edit_secret="s2"))


def test_job_round_trip_and_session_link(db_with_schema):
    """Job rows belong to a session; relationship is navigable both ways."""
    from db.models import Job, Session as SessionRow
    from db.session import get_db

    with get_db() as db:
        session_row = SessionRow(short_code="abc", edit_secret="s")
        db.add(session_row)
        db.flush()
        job = Job(
            session_id=session_row.id,
            kind="find_pockets",
            status="running",
            step="extracting frames",
            celery_task_id="celery-task-1",
            result_info={"frames_extracted": 12},
        )
        db.add(job)
        db.flush()
        jid = job.id

    with get_db() as db:
        loaded = db.get(Job, jid)
        assert loaded is not None
        assert loaded.kind == "find_pockets"
        assert loaded.status == "running"
        assert loaded.result_info == {"frames_extracted": 12}
        # Relationship works
        assert loaded.session.short_code == "abc"


def test_cascade_delete_session_drops_jobs(db_with_schema):
    """Dropping a Session row removes its child Job rows."""
    from sqlalchemy import select

    from db.models import Job, Session as SessionRow
    from db.session import get_db

    with get_db() as db:
        s = SessionRow(short_code="cascade", edit_secret="x")
        db.add(s)
        db.flush()
        db.add(Job(session_id=s.id, kind="cluster"))
        db.flush()
        sid = s.id

    with get_db() as db:
        db.delete(db.get(SessionRow, sid))

    with get_db() as db:
        remaining = db.scalars(select(Job)).all()
        assert remaining == []
