"""Phase C C5 — queue_depth_per_pool, /healthz deep checks, quota task."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── queue_depth_per_pool ────────────────────────────────────────────────


class TestQueueDepthPerPool:
    def test_sums_redis_llen_per_pool(self, monkeypatch):
        from panels import _shared

        # Pretend the broker says: default=3, celery=1, docking=2 → fast=4, docking=2.
        fake_redis = MagicMock()
        fake_redis.llen.side_effect = lambda q: {
            "default": 3, "celery": 1, "docking": 2,
        }.get(q, 0)
        fake_redis_module = MagicMock()
        fake_redis_module.from_url.return_value = fake_redis

        fake_celery_app = MagicMock()
        fake_celery_app.control.inspect.return_value.reserved.return_value = {}
        fake_celery_app.control.inspect.return_value.active_queues.return_value = {}
        fake_module = MagicMock(celery_app=fake_celery_app)

        with patch.dict("sys.modules", {"redis": fake_redis_module,
                                         "celery_app": fake_module}):
            result = _shared.queue_depth_per_pool()
        assert result == {"fast": 4, "docking": 2}

    def test_adds_reserved_tasks_by_worker_queue(self, monkeypatch):
        from panels import _shared

        fake_redis = MagicMock()
        fake_redis.llen.return_value = 0
        fake_redis_module = MagicMock(from_url=MagicMock(return_value=fake_redis))

        fake_inspect = MagicMock()
        fake_inspect.reserved.return_value = {
            "fast-1@host": ["t1", "t2"],
            "docking-1@host": ["t3"],
        }
        fake_inspect.active_queues.return_value = {
            "fast-1@host": [{"name": "default"}],
            "docking-1@host": [{"name": "docking"}],
        }
        fake_celery_app = MagicMock()
        fake_celery_app.control.inspect.return_value = fake_inspect

        with patch.dict("sys.modules", {"redis": fake_redis_module,
                                         "celery_app": MagicMock(celery_app=fake_celery_app)}):
            result = _shared.queue_depth_per_pool()
        assert result == {"fast": 2, "docking": 1}

    def test_degrades_to_zero_on_redis_failure(self):
        from panels import _shared

        # No fake redis injected → import of `redis` falls through to the
        # real package (which is installed) but `from_url` against a bogus
        # broker either succeeds with a stub connection or fails fast.
        # Easier: patch _shared.queue_depth_per_pool via the internals to
        # force the except branch.
        fake_redis_module = MagicMock()
        fake_redis_module.from_url.side_effect = RuntimeError("redis down")
        fake_celery = MagicMock()
        fake_celery.control.inspect.return_value.reserved.return_value = {}
        fake_celery.control.inspect.return_value.active_queues.return_value = {}
        with patch.dict("sys.modules", {"redis": fake_redis_module,
                                         "celery_app": MagicMock(celery_app=fake_celery)}):
            result = _shared.queue_depth_per_pool()
        assert result == {"fast": 0, "docking": 0}


# ── /healthz deep checks ────────────────────────────────────────────────


class TestHealthzDeep:
    def _make_orchestrator(self, pool_ok: bool = True, docker_ok: bool = True):
        """Build a mock orchestrator with overridable Docker reachability."""
        from orchestrator.base import PoolStatus

        orch = MagicMock()
        if pool_ok:
            orch.pool_status.return_value = PoolStatus(
                name="fast", queue="default", desired_size=2,
            )
        else:
            orch.pool_status.side_effect = RuntimeError("pool boom")

        # ``_check_docker`` looks at orch._client.ping().
        client = MagicMock()
        if docker_ok:
            client.ping.return_value = True
        else:
            client.ping.side_effect = RuntimeError("docker down")
        orch._client = client
        return orch

    def _client(self, orch):
        from orchestrator.api import create_app

        app = create_app(orch, ["fast", "docking"])
        app.config["TESTING"] = True
        return app.test_client()

    def test_returns_200_when_all_ok(self, monkeypatch):
        from sqlalchemy import create_engine

        # Stub the DB engine + redis check helpers.
        import orchestrator.api as api_mod
        engine = create_engine("sqlite:///:memory:")
        monkeypatch.setattr(api_mod, "_check_db", lambda: {"ok": True})
        monkeypatch.setattr(api_mod, "_check_redis", lambda: {"ok": True})

        orch = self._make_orchestrator(pool_ok=True, docker_ok=True)
        resp = self._client(orch).get("/healthz")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "ok"
        assert body["checks"]["docker"]["ok"] is True
        assert body["checks"]["redis"]["ok"] is True
        assert body["checks"]["db"]["ok"] is True

    def test_returns_503_when_redis_down(self, monkeypatch):
        import orchestrator.api as api_mod
        monkeypatch.setattr(api_mod, "_check_db", lambda: {"ok": True})
        monkeypatch.setattr(api_mod, "_check_redis", lambda: {"ok": False,
                                                              "error": "ConnectionError"})

        orch = self._make_orchestrator(pool_ok=True, docker_ok=True)
        resp = self._client(orch).get("/healthz")
        assert resp.status_code == 503
        body = resp.get_json()
        assert body["status"] == "unhealthy"
        assert body["checks"]["redis"]["ok"] is False

    def test_returns_503_when_docker_unreachable(self, monkeypatch):
        import orchestrator.api as api_mod
        monkeypatch.setattr(api_mod, "_check_db", lambda: {"ok": True})
        monkeypatch.setattr(api_mod, "_check_redis", lambda: {"ok": True})

        orch = self._make_orchestrator(pool_ok=True, docker_ok=False)
        resp = self._client(orch).get("/healthz")
        assert resp.status_code == 503
        body = resp.get_json()
        assert body["checks"]["docker"]["ok"] is False


# ── enforce_session_disk_quotas_task ────────────────────────────────────


class TestEnforceSessionDiskQuotas:
    def test_no_op_when_no_sessions_over_quota(self, db_with_schema, monkeypatch):
        from cleanup_job import enforce_session_disk_quotas_task
        from db.sessions import create_session

        # Stub disk_usage_mb to always return 0 so nothing is over quota.
        import db.sessions as _sessions_mod
        monkeypatch.setattr(_sessions_mod, "disk_usage_mb",
                            lambda *a, **kw: 0.0)
        create_session()
        result = enforce_session_disk_quotas_task()
        assert result["status"] == "success"
        assert result["sessions_pruned"] == 0

    def test_prunes_oldest_job_dir_when_over_quota(self, db_with_schema, tmp_path,
                                                    monkeypatch):
        import datetime as _dt

        from db.jobs import create_for_legacy
        from db.models import Job
        from db.session import get_db
        from db.sessions import create_session
        from settings import settings
        from sqlalchemy import select

        # Point the suite at our tmp dirs so prune writes don't escape.
        monkeypatch.setattr(settings, "RESULTS_DIR", tmp_path / "r")
        monkeypatch.setattr(settings, "UPLOAD_DIR", tmp_path / "u")
        from config import Config
        monkeypatch.setattr(Config, "RESULTS_DIR", settings.RESULTS_DIR)
        monkeypatch.setattr(Config, "UPLOAD_DIR", settings.UPLOAD_DIR)
        monkeypatch.setattr(Config, "PER_SESSION_DISK_QUOTA_MB", 1)
        settings.RESULTS_DIR.mkdir()
        settings.UPLOAD_DIR.mkdir()

        s = create_session()
        # Two jobs, 1 MB each → 2 MB total, over the 1 MB cap by 1 MB.
        for legacy_id in ("older", "newer"):
            create_for_legacy(s.id, "find_pockets", legacy_id)
            (settings.RESULTS_DIR / legacy_id).mkdir()
            (settings.RESULTS_DIR / legacy_id / "blob").write_bytes(
                b"x" * (1024 * 1024),
            )
        # Force a clear ordering — without this both rows land in the
        # same millisecond and find_by_session's tie-break is unstable.
        with get_db() as db:
            older = db.scalars(select(Job).where(Job.legacy_id == "older")).one()
            older.created_at = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)

        from cleanup_job import enforce_session_disk_quotas_task
        result = enforce_session_disk_quotas_task()
        assert result["status"] == "success"
        assert result["sessions_pruned"] == 1
        # find_by_session orders newest-first; the task reverses to delete
        # oldest first.
        assert not (settings.RESULTS_DIR / "older").exists()
        assert (settings.RESULTS_DIR / "newer").exists()
