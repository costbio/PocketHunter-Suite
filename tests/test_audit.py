"""Tests for the audit-event recorder (B3.4)."""
from __future__ import annotations

from unittest.mock import patch

import pytest


class TestRecord:
    def test_inserts_row(self, db_with_schema):
        from db import audit
        from db.models import AuditEvent
        from db.session import get_db
        from db.sessions import create_session
        from sqlalchemy import select

        s = create_session()
        audit.record(s.id, "203.0.113.5", "session_create",
                     {"foo": "bar"})

        with get_db() as db:
            rows = list(db.scalars(select(AuditEvent)).all())
        assert len(rows) == 1
        assert rows[0].action == "session_create"
        assert rows[0].ip == "203.0.113.5"
        assert rows[0].session_id == s.id
        assert rows[0].details == {"foo": "bar"}

    def test_accepts_none_details(self, db_with_schema):
        from db import audit
        from db.models import AuditEvent
        from db.session import get_db
        from db.sessions import create_session
        from sqlalchemy import select

        s = create_session()
        audit.record(s.id, "1.2.3.4", "session_create")

        with get_db() as db:
            row = db.scalars(select(AuditEvent)).one()
        assert row.details is None

    def test_accepts_none_session_and_ip(self, db_with_schema):
        from db import audit
        from db.models import AuditEvent
        from db.session import get_db
        from sqlalchemy import select

        # Both nullable per the schema; defensive helper accepts them.
        audit.record(None, None, "weird_event")
        with get_db() as db:
            row = db.scalars(select(AuditEvent)).one()
        assert row.session_id is None
        assert row.ip is None

    def test_never_raises_on_db_failure(self, db_with_schema):
        from db import audit

        # Force get_db to explode and confirm the helper swallows it.
        with patch("db.audit.get_db", side_effect=RuntimeError("DB down")):
            # Must not raise.
            audit.record(None, "1.1.1.1", "session_create", {"x": 1})
