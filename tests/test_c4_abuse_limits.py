"""Phase C C4 tests — abuse limits, disk quota, pool caps, client_ip."""
from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock, patch

import pytest


def _make_job(session_id, kind: str, legacy_id: str, status: str = "submitted"):
    """Create a Job row with the requested status (helper since
    create_for_legacy always defaults to 'submitted')."""
    from db.jobs import create_for_legacy, update_by_legacy_id

    create_for_legacy(session_id, kind, legacy_id)
    if status != "submitted":
        update_by_legacy_id(legacy_id, status)


# ── client_ip ───────────────────────────────────────────────────────────


class TestClientIp:
    def test_prefers_x_forwarded_for_first_hop(self):
        from client_ip import client_ip

        fake_st = MagicMock()
        fake_st.context.headers = {"X-Forwarded-For": "203.0.113.5, 10.0.0.1, 10.0.0.2"}
        with patch.dict("sys.modules", {"streamlit": fake_st}):
            assert client_ip() == "203.0.113.5"

    def test_falls_back_to_x_real_ip(self):
        from client_ip import client_ip

        fake_st = MagicMock()
        fake_st.context.headers = {"X-Real-IP": "198.51.100.7"}
        with patch.dict("sys.modules", {"streamlit": fake_st}):
            assert client_ip() == "198.51.100.7"

    def test_no_headers_returns_unknown(self):
        from client_ip import client_ip

        fake_st = MagicMock()
        fake_st.context.headers = {}
        with patch.dict("sys.modules", {"streamlit": fake_st}):
            assert client_ip() == "unknown"

    def test_handles_missing_context_gracefully(self):
        from client_ip import client_ip

        fake_st = MagicMock()
        del fake_st.context  # AttributeError path
        with patch.dict("sys.modules", {"streamlit": fake_st}):
            assert client_ip() == "unknown"


# ── disk_usage_mb ───────────────────────────────────────────────────────


class TestDiskUsageMb:
    def test_returns_zero_for_session_with_no_jobs(self, db_with_schema):
        from db.sessions import create_session, disk_usage_mb

        s = create_session()
        assert disk_usage_mb(s.id) == 0.0

    def test_sums_results_and_uploads_dirs(self, db_with_schema, tmp_path, monkeypatch):
        from db.sessions import create_session, disk_usage_mb

        results_dir = tmp_path / "results"
        uploads_dir = tmp_path / "uploads"
        results_dir.mkdir()
        uploads_dir.mkdir()

        # Two megabytes worth of data across two jobs.
        s = create_session()
        for legacy_id, size_mb in [("job_a", 1), ("job_b", 1)]:
            _make_job(s.id, "find_pockets", legacy_id)
            jr = results_dir / legacy_id
            jr.mkdir()
            (jr / "blob").write_bytes(b"x" * (size_mb * 1024 * 1024))
            ju = uploads_dir / legacy_id
            ju.mkdir()
            (ju / "in").write_bytes(b"y" * (512 * 1024))  # 0.5 MB

        # Redirect the settings paths so the helper looks where we wrote.
        from settings import settings

        monkeypatch.setattr(settings, "RESULTS_DIR", results_dir)
        monkeypatch.setattr(settings, "UPLOAD_DIR", uploads_dir)
        assert disk_usage_mb(s.id) == pytest.approx(3.0, abs=0.05)

    def test_missing_dirs_contribute_zero(self, db_with_schema, tmp_path, monkeypatch):
        from db.sessions import create_session, disk_usage_mb
        from settings import settings

        monkeypatch.setattr(settings, "RESULTS_DIR", tmp_path / "nope")
        monkeypatch.setattr(settings, "UPLOAD_DIR", tmp_path / "nope2")
        s = create_session()
        _make_job(s.id, "find_pockets", "ghost")
        assert disk_usage_mb(s.id) == 0.0


# ── in_flight_count ─────────────────────────────────────────────────────


class TestInFlightCount:
    def test_zero_for_empty_db(self, db_with_schema):
        from db.jobs import in_flight_count

        assert in_flight_count() == 0

    def test_counts_only_unfinished_statuses(self, db_with_schema):
        from db.jobs import in_flight_count
        from db.sessions import create_session

        s = create_session()
        _make_job(s.id, "find_pockets", "r1", status="running")
        _make_job(s.id, "find_pockets", "r2", status="submitted")
        _make_job(s.id, "find_pockets", "done", status="completed")
        _make_job(s.id, "find_pockets", "fail", status="failed")
        assert in_flight_count() == 2

    def test_filters_by_pool(self, db_with_schema):
        from db.jobs import in_flight_count
        from db.sessions import create_session

        s = create_session()
        _make_job(s.id, "find_pockets", "fp1", status="running")
        _make_job(s.id, "cluster", "c1", status="running")
        _make_job(s.id, "docking", "d1", status="running")
        assert in_flight_count(pool="fast") == 2  # find_pockets + cluster
        assert in_flight_count(pool="docking") == 1

    def test_filters_by_session(self, db_with_schema):
        from db.jobs import in_flight_count
        from db.sessions import create_session

        s_a = create_session()
        s_b = create_session()
        _make_job(s_a.id, "find_pockets", "a1", status="running")
        _make_job(s_b.id, "find_pockets", "b1", status="running")
        _make_job(s_b.id, "find_pockets", "b2", status="submitted")
        assert in_flight_count(session_id=s_a.id) == 1
        assert in_flight_count(session_id=s_b.id) == 2

    def test_unknown_pool_raises(self, db_with_schema):
        from db.jobs import in_flight_count

        with pytest.raises(ValueError, match="Unknown pool"):
            in_flight_count(pool="bogus")


# ── assert_submit_allowed / PoolCapHit ──────────────────────────────────


class TestAssertSubmitAllowed:
    def test_passes_under_caps(self, db_with_schema, monkeypatch):
        from config import Config
        from panels._shared import assert_submit_allowed

        monkeypatch.setattr(Config, "RATE_LIMIT_ENABLED", True)
        # No jobs at all → must not raise.
        assert_submit_allowed("fast", session_id=None)

    def test_global_cap_raises(self, db_with_schema, monkeypatch):
        from config import Config
        from db.sessions import create_session
        from panels._shared import PoolCapHit, assert_submit_allowed

        monkeypatch.setattr(Config, "RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(Config, "MAX_CONCURRENT_FAST_JOBS", 1)
        s = create_session()
        _make_job(s.id, "find_pockets", "busy", status="running")
        with pytest.raises(PoolCapHit) as ex:
            assert_submit_allowed("fast", session_id=None)
        assert ex.value.scope == "global"

    def test_per_session_cap_raises(self, db_with_schema, monkeypatch):
        from config import Config
        from db.sessions import create_session
        from panels._shared import PoolCapHit, assert_submit_allowed

        monkeypatch.setattr(Config, "RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(Config, "MAX_CONCURRENT_FAST_JOBS", 100)
        monkeypatch.setattr(Config, "MAX_CONCURRENT_FAST_PER_SESSION", 1)
        s = create_session()
        _make_job(s.id, "find_pockets", "mine", status="running")
        with pytest.raises(PoolCapHit) as ex:
            assert_submit_allowed("fast", session_id=s.id)
        assert ex.value.scope == "session"

    def test_no_op_when_rate_limiting_disabled(self, db_with_schema, monkeypatch):
        from config import Config
        from db.sessions import create_session
        from panels._shared import assert_submit_allowed

        monkeypatch.setattr(Config, "RATE_LIMIT_ENABLED", False)
        monkeypatch.setattr(Config, "MAX_CONCURRENT_FAST_JOBS", 0)
        s = create_session()
        _make_job(s.id, "find_pockets", "any", status="running")
        # Would otherwise fire — but the flag is off.
        assert_submit_allowed("fast", session_id=s.id)


# ── SessionQuotaExceeded path ───────────────────────────────────────────


class TestSessionQuotaExceeded:
    def test_handle_upload_rejects_over_quota(self, db_with_schema, monkeypatch, tmp_path):
        from config import Config
        from db.sessions import create_session
        from security import SessionQuotaExceeded, handle_file_upload_secure

        monkeypatch.setattr(Config, "RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(Config, "PER_SESSION_DISK_QUOTA_MB", 1)
        # Mock disk_usage_mb so we don't have to materialise a giant fake file.
        import db.sessions as _sessions_mod

        monkeypatch.setattr(_sessions_mod, "disk_usage_mb", lambda *a, **kw: 1.0)

        s = create_session()
        uploaded = MagicMock()
        uploaded.size = 3 * 1024 * 1024  # 3 MB
        uploaded.name = "trajectory.xtc"
        uploaded.getbuffer = lambda: b"x" * uploaded.size
        with pytest.raises(SessionQuotaExceeded) as ex:
            handle_file_upload_secure(uploaded, "j1", session_id=s.id)
        assert "quota" in str(ex.value).lower()
