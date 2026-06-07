"""Tests for the DB-driven per-IP daily session-create rate limit.

Semantics under test (the "only committed sessions count" rule):

* A session that has ≥1 job row → counts toward the per-IP cap.
* A session with NO jobs → does NOT count, regardless of age. The
  ``SESSION_GRACE_MINUTES`` window only governs when the cleanup
  task is allowed to delete the empty session, not whether it's
  counted by the rate limit.

Each test gets a fresh in-memory SQLite via the ``db_with_schema``
fixture; pre-seeded audit + session + job rows simulate the various
counted/uncounted states.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


import uuid as _uuid


def _seed(db, *, ip, age_minutes=0, has_job=False, action="session_create"):
    """Insert a Session, an AuditEvent(action, ip) backdated by
    ``age_minutes``, and optionally a Job row. Returns the Session."""
    from db.models import AuditEvent, Job, Session

    created = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    s = Session(
        short_code=f"sh-{_uuid.uuid4().hex[:10]}",
        edit_secret="x" * 32,
        state={},
    )
    db.add(s)
    db.flush()
    # SQLite stores naive datetimes for our schema columns — drop tz to match.
    s.created_at = created.replace(tzinfo=None)
    db.flush()

    ae = AuditEvent(session_id=s.id, ip=ip, action=action)
    db.add(ae)
    db.flush()
    ae.created_at = created.replace(tzinfo=None)
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
def rate_limit_on(monkeypatch):
    """check_session_create_rate_limit short-circuits when
    RATE_LIMIT_ENABLED=false; force it true for the test."""
    monkeypatch.setattr("config.Config.RATE_LIMIT_ENABLED", True)


@pytest.fixture
def cap_3(monkeypatch):
    """Tiny cap so we don't have to seed 20 sessions per test."""
    monkeypatch.setattr("config.Config.MAX_SESSIONS_PER_IP_PER_DAY", 3)


@pytest.fixture
def grace_15(monkeypatch):
    monkeypatch.setattr("config.Config.SESSION_GRACE_MINUTES", 15)


class TestCheckSessionCreateRateLimit:
    def test_no_sessions_no_block(self, db_with_schema, rate_limit_on, cap_3, grace_15):
        from rate_limiter import check_session_create_rate_limit
        # Empty DB → no rate limit triggered.
        check_session_create_rate_limit("203.0.113.1")  # no raise = pass

    def test_abandoned_old_sessions_dont_count(self, db_with_schema, rate_limit_on,
                                               cap_3, grace_15):
        """Five empty old sessions for this IP → should NOT block.
        These would be deleted by the cleanup task; until then they
        must not block new creations either."""
        from db.session import get_db
        from rate_limiter import check_session_create_rate_limit

        ip = "203.0.113.10"
        with get_db() as db:
            for _ in range(5):
                _seed(db, ip=ip, age_minutes=60, has_job=False)

        check_session_create_rate_limit(ip)  # no raise

    def test_grace_window_sessions_dont_count(self, db_with_schema, rate_limit_on,
                                              cap_3, grace_15):
        """Fresh empty sessions (< grace) do NOT count — only jobs
        committed by the user count toward the per-IP cap. Opening 20
        new-session tabs and walking away must not block the next
        legitimate visit."""
        from db.session import get_db
        from rate_limiter import check_session_create_rate_limit

        ip = "203.0.113.20"
        with get_db() as db:
            for _ in range(10):
                _seed(db, ip=ip, age_minutes=5, has_job=False)  # within grace

        check_session_create_rate_limit(ip)  # no raise — none committed

    def test_committed_sessions_count(self, db_with_schema, rate_limit_on,
                                      cap_3, grace_15):
        """Sessions with at least one Job row always count, regardless of age."""
        from db.session import get_db
        from rate_limiter import RateLimitExceeded, check_session_create_rate_limit

        ip = "203.0.113.30"
        with get_db() as db:
            for _ in range(3):
                _seed(db, ip=ip, age_minutes=120, has_job=True)  # old + committed

        with pytest.raises(RateLimitExceeded):
            check_session_create_rate_limit(ip)

    def test_other_ips_not_counted(self, db_with_schema, rate_limit_on, cap_3, grace_15):
        """The cap is per-IP."""
        from db.session import get_db
        from rate_limiter import check_session_create_rate_limit

        with get_db() as db:
            for _ in range(10):
                _seed(db, ip="198.51.100.1", age_minutes=5, has_job=True)

        # Different IP → not blocked.
        check_session_create_rate_limit("203.0.113.99")

    def test_old_creations_outside_24h_not_counted(self, db_with_schema, rate_limit_on,
                                                   cap_3, grace_15):
        """Yesterday's committed sessions don't count toward today's cap."""
        from db.session import get_db
        from rate_limiter import check_session_create_rate_limit

        ip = "203.0.113.40"
        with get_db() as db:
            for _ in range(5):
                _seed(db, ip=ip, age_minutes=60 * 25, has_job=True)  # >24h ago

        check_session_create_rate_limit(ip)  # no raise

    def test_mix_grace_plus_committed_under_cap(self, db_with_schema, rate_limit_on,
                                                cap_3, grace_15):
        """Grace-window sessions never count; only committed do. 5
        empty + 2 committed = 2 < cap=3 → allowed."""
        from db.session import get_db
        from rate_limiter import check_session_create_rate_limit

        ip = "203.0.113.50"
        with get_db() as db:
            for _ in range(5):
                _seed(db, ip=ip, age_minutes=2, has_job=False)
            for _ in range(2):
                _seed(db, ip=ip, age_minutes=60, has_job=True)

        check_session_create_rate_limit(ip)  # no raise

    def test_disabled_short_circuits(self, db_with_schema, cap_3, grace_15, monkeypatch):
        """RATE_LIMIT_ENABLED=false → never raises, even with seeded blockers."""
        monkeypatch.setattr("config.Config.RATE_LIMIT_ENABLED", False)
        from db.session import get_db
        from rate_limiter import check_session_create_rate_limit

        ip = "203.0.113.60"
        with get_db() as db:
            for _ in range(20):
                _seed(db, ip=ip, age_minutes=2, has_job=True)

        check_session_create_rate_limit(ip)  # no raise
