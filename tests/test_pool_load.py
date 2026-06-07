"""Tests for ``pool_load.compute_pool_load`` and
``db.jobs.mean_recent_duration_seconds``.

Covers:
* empty DB → both pools idle, no wait
* in-flight count under capacity → busy=N, queued=0
* in-flight count over capacity → workers_busy capped, queued positive,
  est_wait derived from mean recent duration
* per-pool isolation (fast vs docking)
* mean-duration helper returns the expected average
* mean-duration helper bounds by ``hours`` lookback window
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest


def _seed_job(db, *, kind, status, created_minutes_ago=0,
              duration_seconds=0):
    """Insert a Job with controllable created_at / updated_at."""
    from db.models import Job, Session

    # Need a session row to satisfy the FK.
    sess = Session(
        short_code=f"pl-{_uuid.uuid4().hex[:10]}",
        edit_secret="x" * 32,
        state={},
    )
    db.add(sess)
    db.flush()

    created = datetime.now(timezone.utc) - timedelta(minutes=created_minutes_ago)
    updated = created + timedelta(seconds=duration_seconds)

    j = Job(
        session_id=sess.id,
        kind=kind,
        status=status,
        legacy_id=f"{kind}-{_uuid.uuid4().hex[:8]}",
    )
    db.add(j)
    db.flush()
    # SQLite stores naive datetimes for these columns.
    j.created_at = created.replace(tzinfo=None)
    j.updated_at = updated.replace(tzinfo=None)
    db.flush()
    db.commit()
    return j


@pytest.fixture
def pool_sizes(monkeypatch):
    monkeypatch.setattr("config.Config.FAST_POOL_SIZE", 6)
    monkeypatch.setattr("config.Config.DOCKING_POOL_SIZE", 3)


class TestComputePoolLoad:
    def test_empty_db_is_idle(self, db_with_schema, pool_sizes):
        from pool_load import compute_pool_load

        fast = compute_pool_load("fast")
        dock = compute_pool_load("docking")
        assert fast.workers_busy == 0
        assert fast.queued == 0
        assert fast.est_wait_seconds == 0
        assert fast.workers_total == 6
        assert dock.workers_busy == 0
        assert dock.queued == 0
        assert dock.workers_total == 3

    def test_under_capacity(self, db_with_schema, pool_sizes):
        """3 in-flight fast jobs, pool size 6 → busy=3, queued=0."""
        from db.session import get_db
        from pool_load import compute_pool_load

        with get_db() as db:
            for _ in range(3):
                _seed_job(db, kind="find_pockets", status="running")

        fast = compute_pool_load("fast")
        assert fast.workers_busy == 3
        assert fast.queued == 0
        assert fast.est_wait_seconds == 0

    def test_over_capacity_has_queue_and_wait(self, db_with_schema, pool_sizes):
        """8 in-flight + 6 historical 60s jobs → busy=6, queued=2,
        est_wait = 2 * 60 // 6 = 20s."""
        from db.session import get_db
        from pool_load import compute_pool_load

        with get_db() as db:
            for _ in range(8):
                _seed_job(db, kind="find_pockets", status="running")
            for _ in range(5):
                _seed_job(db, kind="find_pockets", status="completed",
                          created_minutes_ago=30, duration_seconds=60)

        fast = compute_pool_load("fast")
        assert fast.workers_busy == 6
        assert fast.queued == 2
        assert fast.avg_job_seconds == 60
        assert fast.est_wait_seconds == 20

    def test_queue_but_no_history_means_zero_wait(self, db_with_schema, pool_sizes):
        """If we have queued jobs but no completed-job history,
        est_wait is 0 (we don't fabricate a guess)."""
        from db.session import get_db
        from pool_load import compute_pool_load

        with get_db() as db:
            for _ in range(8):
                _seed_job(db, kind="find_pockets", status="running")

        fast = compute_pool_load("fast")
        assert fast.queued == 2
        assert fast.avg_job_seconds == 0
        assert fast.est_wait_seconds == 0

    def test_pools_isolated(self, db_with_schema, pool_sizes):
        """Docking jobs don't bleed into fast counts and vice versa."""
        from db.session import get_db
        from pool_load import compute_pool_load

        with get_db() as db:
            for _ in range(2):
                _seed_job(db, kind="docking", status="running")
            for _ in range(4):
                _seed_job(db, kind="find_pockets", status="running")

        fast = compute_pool_load("fast")
        dock = compute_pool_load("docking")
        assert fast.workers_busy == 4
        assert dock.workers_busy == 2

    def test_cluster_kind_counts_as_fast(self, db_with_schema, pool_sizes):
        """The 'cluster' kind shares the fast pool with find_pockets."""
        from db.session import get_db
        from pool_load import compute_pool_load

        with get_db() as db:
            _seed_job(db, kind="cluster", status="running")
            _seed_job(db, kind="find_pockets", status="running")

        assert compute_pool_load("fast").workers_busy == 2
        assert compute_pool_load("docking").workers_busy == 0

    def test_unknown_pool_raises(self, db_with_schema, pool_sizes):
        from pool_load import compute_pool_load
        with pytest.raises(ValueError, match="Unknown pool"):
            compute_pool_load("bogus")


class TestMeanRecentDurationSeconds:
    def test_no_completed_returns_zero(self, db_with_schema):
        from db.jobs import mean_recent_duration_seconds
        assert mean_recent_duration_seconds(pool="fast") == 0
        assert mean_recent_duration_seconds(pool="docking") == 0

    def test_returns_mean_for_pool(self, db_with_schema):
        """Seed 4 completed fast jobs with mixed durations → mean."""
        from db.jobs import mean_recent_duration_seconds
        from db.session import get_db

        with get_db() as db:
            for s in (30, 60, 90, 120):
                _seed_job(db, kind="find_pockets", status="completed",
                          created_minutes_ago=10, duration_seconds=s)

        assert mean_recent_duration_seconds(pool="fast") == 75

    def test_lookback_window_excludes_old(self, db_with_schema):
        """Jobs older than the lookback window don't count."""
        from db.jobs import mean_recent_duration_seconds
        from db.session import get_db

        with get_db() as db:
            # Inside 24h window
            _seed_job(db, kind="find_pockets", status="completed",
                      created_minutes_ago=60, duration_seconds=100)
            # Outside 24h window (25h ago)
            _seed_job(db, kind="find_pockets", status="completed",
                      created_minutes_ago=60 * 25, duration_seconds=600)

        # Only the 100s job should count.
        assert mean_recent_duration_seconds(pool="fast", hours=24) == 100

    def test_skips_running_and_failed(self, db_with_schema):
        """Only ``status='completed'`` jobs contribute."""
        from db.jobs import mean_recent_duration_seconds
        from db.session import get_db

        with get_db() as db:
            _seed_job(db, kind="find_pockets", status="completed",
                      created_minutes_ago=10, duration_seconds=60)
            _seed_job(db, kind="find_pockets", status="running",
                      created_minutes_ago=10, duration_seconds=600)
            _seed_job(db, kind="find_pockets", status="failed",
                      created_minutes_ago=10, duration_seconds=600)

        assert mean_recent_duration_seconds(pool="fast") == 60
