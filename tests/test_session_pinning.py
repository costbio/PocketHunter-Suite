"""Tests for pinned sessions — the exemption that keeps the published demo alive.

A pinned session has to survive all three sweeps that can destroy a
session's work:

* ``cleanup_abandoned_sessions_task`` deletes empty sessions past the grace
  window (a pinned session should never be reaped even if it has no jobs
  yet, e.g. between pinning and the first run).
* ``enforce_session_disk_quotas_task`` prunes a session's oldest job
  artefacts when it exceeds the per-session quota.
* ``ResourceManager.cleanup_old_jobs`` deletes job directories by mtime and
  knows nothing about the database — the risky one, since it rmtree's.

The last test is the one that matters most: it asserts the task refuses to
sweep at all when it cannot learn which jobs are protected. Deleting a
pinned session's artefacts is unrecoverable; skipping a sweep is not.
"""
from __future__ import annotations

import os
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


def _seed_session(db, *, pinned=False, legacy_ids=()):
    from db.models import Job, Session

    s = Session(
        short_code=f"pin-{_uuid.uuid4().hex[:8]}",
        edit_secret="x" * 32,
        state={},
        pinned=pinned,
    )
    db.add(s)
    db.flush()
    for lid in legacy_ids:
        db.add(Job(session_id=s.id, kind="find_pockets",
                   status="completed", legacy_id=lid))
    db.flush()
    db.commit()
    return s


class TestPinnedJobLegacyIds:
    def test_returns_only_pinned_sessions_jobs(self, db_with_schema):
        from db.session import get_db
        from db.sessions import pinned_job_legacy_ids

        with get_db() as db:
            _seed_session(db, pinned=True, legacy_ids=["keep-1", "keep-2"])
            _seed_session(db, pinned=False, legacy_ids=["drop-1"])

        assert pinned_job_legacy_ids() == {"keep-1", "keep-2"}

    def test_empty_when_nothing_pinned(self, db_with_schema):
        from db.session import get_db
        from db.sessions import pinned_job_legacy_ids

        with get_db() as db:
            _seed_session(db, pinned=False, legacy_ids=["drop-1"])

        assert pinned_job_legacy_ids() == set()

    def test_sessions_default_to_unpinned(self, db_with_schema):
        """A column added late must not silently pin existing rows."""
        from db.models import Session
        from db.session import get_db

        with get_db() as db:
            s = Session(short_code=f"d-{_uuid.uuid4().hex[:8]}",
                        edit_secret="y" * 32, state={})
            db.add(s)
            db.flush()
            assert s.pinned is False


class TestAbandonedSessionSweep:
    def test_pinned_empty_session_survives(self, db_with_schema, monkeypatch):
        from cleanup_job import cleanup_abandoned_sessions_task
        from db.models import Session
        from db.session import get_db
        from sqlalchemy import select

        monkeypatch.setattr("config.Config.SESSION_GRACE_MINUTES", 15)
        old = datetime.now(timezone.utc) - timedelta(minutes=90)

        with get_db() as db:
            pinned = _seed_session(db, pinned=True)
            plain = _seed_session(db, pinned=False)
            for row in (pinned, plain):
                row.created_at = old.replace(tzinfo=None)
            db.commit()
            pinned_id, plain_id = pinned.id, plain.id

        result = cleanup_abandoned_sessions_task()
        assert result["status"] == "success"

        with get_db() as db:
            assert db.scalars(
                select(Session).where(Session.id == pinned_id)
            ).one_or_none() is not None
            assert db.scalars(
                select(Session).where(Session.id == plain_id)
            ).one_or_none() is None


class TestCleanupOldJobs:
    """ResourceManager.cleanup_old_jobs deletes by mtime; protection is by id."""

    def _age(self, path, days):
        old = (datetime.now() - timedelta(days=days)).timestamp()
        os.utime(path, (old, old))

    @pytest.fixture
    def dirs(self, tmp_path, monkeypatch):
        uploads = tmp_path / "uploads"
        results = tmp_path / "results"
        uploads.mkdir()
        results.mkdir()
        monkeypatch.setattr("config.Config.UPLOAD_DIR", uploads)
        monkeypatch.setattr("config.Config.RESULTS_DIR", results)
        monkeypatch.setattr("config.Config.CLEANUP_AFTER_DAYS", 30)
        return uploads, results

    def test_protected_dir_survives_while_its_neighbour_goes(self, dirs):
        from resource_manager import ResourceManager

        uploads, results = dirs
        for parent in (uploads, results):
            for name in ("pinned-job", "stale-job"):
                d = parent / name
                d.mkdir()
                (d / "artefact.txt").write_text("x")
                self._age(d, days=90)

        deleted = ResourceManager.cleanup_old_jobs(
            dry_run=False, protected_job_ids={"pinned-job"},
        )

        assert deleted == ["stale-job"]
        assert (uploads / "pinned-job").exists()
        assert (results / "pinned-job").exists()
        assert not (uploads / "stale-job").exists()
        assert not (results / "stale-job").exists()

    def test_without_protection_everything_old_goes(self, dirs):
        """Guards the default: the new parameter must not change old behaviour."""
        from resource_manager import ResourceManager

        uploads, _results = dirs
        d = uploads / "stale-job"
        d.mkdir()
        (d / "a.txt").write_text("x")
        self._age(d, days=90)

        assert ResourceManager.cleanup_old_jobs(dry_run=False) == ["stale-job"]
        assert not d.exists()


class TestCleanupTaskFailsSafe:
    def test_skips_the_sweep_when_pinned_lookup_fails(self):
        """A DB outage must not be read as 'nothing is protected'."""
        from cleanup_job import cleanup_old_jobs_task

        with patch("db.sessions.pinned_job_legacy_ids",
                   side_effect=RuntimeError("db down")), \
             patch("resource_manager.ResourceManager.cleanup_old_jobs") as sweep:
            result = cleanup_old_jobs_task()

        sweep.assert_not_called()
        assert result["status"] == "error"
        assert result["deleted_count"] == 0
        assert "pinned-job lookup failed" in result["error"]

    def test_passes_the_protection_set_through(self, db_with_schema):
        from cleanup_job import cleanup_old_jobs_task
        from db.session import get_db

        with get_db() as db:
            _seed_session(db, pinned=True, legacy_ids=["demo-fp", "demo-dock"])

        with patch("resource_manager.ResourceManager.cleanup_old_jobs",
                   return_value=[]) as sweep:
            cleanup_old_jobs_task()

        assert sweep.call_args.kwargs["protected_job_ids"] == {"demo-fp", "demo-dock"}
