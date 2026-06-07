"""Tests for the orchestrator HTTP API."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orchestrator.api import create_app
from orchestrator.base import (
    Orchestrator,
    PoolStatus,
    WorkerInfo,
    WorkerState,
)


class _FakeOrchestrator(Orchestrator):
    """Minimal in-memory backend so the API can be exercised without Docker."""

    def __init__(self):
        self.pools: dict[str, PoolStatus] = {}

    def start_pool(self, name, size, queue):
        status = PoolStatus(name=name, queue=queue, desired_size=size)
        self.pools[name] = status
        return status

    def pool_status(self, name):
        if name not in self.pools:
            raise KeyError(name)
        return self.pools[name]

    def recycle(self, worker_id):
        for status in self.pools.values():
            for w in status.workers:
                if w.worker_id == worker_id:
                    new = WorkerInfo(
                        worker_id=worker_id + "-new",
                        container_id="c-new",
                        pool=w.pool, queue=w.queue,
                        state=WorkerState.STARTING,
                    )
                    status.workers.remove(w)
                    status.workers.append(new)
                    return new
        raise KeyError(worker_id)

    def reap_orphans(self):
        return []

    def terminate_pool(self, name):
        return 0

    def reconcile(self):
        return {}


@pytest.fixture
def client():
    orch = _FakeOrchestrator()
    orch.start_pool("fast", 2, "default,celery")
    orch.start_pool("docking", 1, "docking")
    # Plant a worker so recycle has something to recycle.
    orch.pools["fast"].workers.append(WorkerInfo(
        worker_id="w1", container_id="c1", pool="fast", queue="default,celery",
        state=WorkerState.READY,
    ))
    app = create_app(orch, ["fast", "docking"])
    app.config["TESTING"] = True
    return app.test_client()


class TestHealthz:
    def test_returns_ok_when_pools_exist(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "ok"


class TestPoolStatus:
    def test_returns_both_pools(self, client):
        resp = client.get("/pool/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert set(data.keys()) == {"fast", "docking"}
        assert data["fast"]["desired_size"] == 2
        assert data["docking"]["queue"] == "docking"

    def test_worker_serialised_with_string_state(self, client):
        resp = client.get("/pool/status")
        data = resp.get_json()
        assert data["fast"]["workers"][0]["state"] == "ready"


class TestPoolRecycle:
    def test_recycle_replaces_worker(self, client):
        resp = client.post("/pool/recycle/w1")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["recycled"] == "w1"
        assert body["replacement"]["worker_id"] == "w1-new"

    def test_recycle_unknown_returns_404(self, client):
        resp = client.post("/pool/recycle/missing")
        assert resp.status_code == 404


class TestEmptyPoolShape:
    """When a pool has never been started, /pool/status still emits a flat shape."""

    def test_unstarted_pool_returns_zero_workers(self):
        orch = _FakeOrchestrator()
        app = create_app(orch, ["fast", "docking"])
        app.config["TESTING"] = True
        c = app.test_client()
        data = c.get("/pool/status").get_json()
        for name in ("fast", "docking"):
            assert data[name]["desired_size"] == 0
            assert data[name]["workers"] == []
            assert data[name]["started"] is False
