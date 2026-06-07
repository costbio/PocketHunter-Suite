"""Tests for the Docker SDK orchestrator backend.

The real Docker daemon (or socket proxy) is replaced with a mock client
exposing the same surface ``DockerSdkOrchestrator`` uses:
``client.containers.run`` / ``.list`` / ``.get``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orchestrator.base import WorkerState
from orchestrator.docker_sdk import (
    DockerSdkOrchestrator,
    LABEL_POOL,
    LABEL_QUEUE,
    LABEL_ROLE,
    LABEL_WORKER_ID,
    ROLE_WORKER,
    _nano_cpus,
)


# ── Fixtures ────────────────────────────────────────────────────────────


def _make_container(container_id: str, status: str = "running",
                    labels: dict | None = None):
    """Stand-in for a docker.models.containers.Container."""
    c = MagicMock()
    c.id = container_id
    c.status = status
    c.labels = labels or {}
    return c


@pytest.fixture
def client():
    """Mock Docker client whose ``containers.run`` returns a fresh container."""
    client = MagicMock()
    client.containers.list.return_value = []
    counter = {"n": 0}

    def _run(*args, **kwargs):
        counter["n"] += 1
        cid = f"cid-{counter['n']:03d}"
        return _make_container(cid, labels=kwargs.get("labels", {}))

    client.containers.run.side_effect = _run
    client.containers.get.side_effect = lambda cid: _make_container(cid)
    return client


@pytest.fixture
def orch(client):
    return DockerSdkOrchestrator(
        docker_url="tcp://docker-proxy:2375",
        worker_image="pockethunter-worker:latest",
        worker_cpu_limit=6,
        worker_memory_limit="12g",
        worker_jobs_before_recycle=10,
        worker_drain_timeout=60,  # short drain in tests
        worker_network="pockethunter_internal",
        host_repo_path="/host/app",
        host_uploads_path="/host/app/uploads",
        host_results_path="/host/app/results",
        host_logs_path="/host/app/logs",
        celery_broker_url="redis://redis:6379/0",
        celery_result_backend="redis://redis:6379/0",
        database_url="postgresql+psycopg://u:p@postgres:5432/db",
        base_url="http://localhost:8511",
        client=client,
    )


# ── Helpers ─────────────────────────────────────────────────────────────


class TestHelpers:
    def test_nano_cpus_converts_to_billionths(self):
        assert _nano_cpus(6) == 6_000_000_000
        assert _nano_cpus(1) == 1_000_000_000


# ── start_pool ──────────────────────────────────────────────────────────


class TestStartPool:
    def test_spawns_n_workers(self, orch, client):
        status = orch.start_pool("fast", size=3, queue="default,celery")
        assert client.containers.run.call_count == 3
        assert status.desired_size == 3
        assert len(status.workers) == 3

    def test_passes_hardening_flags_to_run(self, orch, client):
        orch.start_pool("fast", size=1, queue="default,celery")
        kwargs = client.containers.run.call_args.kwargs

        assert kwargs["read_only"] is True
        assert kwargs["cap_drop"] == ["ALL"]
        assert kwargs["security_opt"] == ["no-new-privileges:true"]
        assert kwargs["user"] == "1000:1000"
        assert kwargs["tmpfs"]["/tmp"] == "exec,size=1g"
        assert kwargs["tmpfs"]["/scratch"] == "size=4g"
        assert kwargs["mem_limit"] == "12g"
        assert kwargs["nano_cpus"] == 6_000_000_000
        assert kwargs["network"] == "pockethunter_internal"
        assert kwargs["init"] is True
        assert kwargs["restart_policy"] == {"Name": "no"}

    def test_labels_each_container_for_reaper(self, orch, client):
        orch.start_pool("fast", size=1, queue="default,celery")
        labels = client.containers.run.call_args.kwargs["labels"]
        assert labels[LABEL_ROLE] == ROLE_WORKER
        assert labels[LABEL_POOL] == "fast"
        assert labels[LABEL_QUEUE] == "default,celery"
        assert labels[LABEL_WORKER_ID].startswith("fast-")

    def test_command_includes_queue_arg(self, orch, client):
        orch.start_pool("docking", size=1, queue="docking")
        cmd = client.containers.run.call_args.kwargs["command"]
        assert "-Q" in cmd
        assert "docking" in cmd
        assert "--concurrency=1" in cmd

    def test_idempotent(self, orch, client):
        orch.start_pool("fast", size=2, queue="default,celery")
        orch.start_pool("fast", size=2, queue="default,celery")
        # Second call should not spawn additional workers.
        assert client.containers.run.call_count == 2

    def test_passes_writable_tmp_env(self, orch, client):
        orch.start_pool("fast", size=1, queue="default,celery")
        env = client.containers.run.call_args.kwargs["environment"]
        assert env["HOME"] == "/tmp"
        assert env["MPLCONFIGDIR"] == "/tmp/matplotlib"
        assert env["XDG_CACHE_HOME"] == "/tmp/cache"
        assert env["XDG_CONFIG_HOME"] == "/tmp/config"
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"

    def test_uploads_mounted_readonly(self, orch, client):
        orch.start_pool("fast", size=1, queue="default,celery")
        volumes = client.containers.run.call_args.kwargs["volumes"]
        assert volumes["/host/app/uploads"]["mode"] == "ro"
        assert volumes["/host/app/results"]["mode"] == "rw"
        assert volumes["/host/app"]["mode"] == "ro"


# ── pool_status ─────────────────────────────────────────────────────────


class TestPoolStatus:
    def test_unknown_pool_raises(self, orch):
        with pytest.raises(KeyError):
            orch.pool_status("nope")

    def test_marks_dead_when_container_not_running(self, orch, client):
        orch.start_pool("fast", size=1, queue="default,celery")
        # Flip the mock get() to return a stopped container.
        client.containers.get.side_effect = lambda cid: _make_container(cid, status="exited")
        status = orch.pool_status("fast")
        assert status.workers[0].state == WorkerState.DEAD


# ── recycle ─────────────────────────────────────────────────────────────


class TestRecycle:
    def test_tears_down_and_replaces(self, orch, client):
        orch.start_pool("fast", size=1, queue="default,celery")
        original = orch.pool_status("fast").workers[0]
        new = orch.recycle(original.worker_id)
        assert new.worker_id != original.worker_id
        assert new.pool == "fast"
        assert new.queue == "default,celery"
        # Old container should have been remove()d.
        # client.containers.get(...).remove was called via _tear_down_container.

    def test_unknown_worker_raises(self, orch):
        orch.start_pool("fast", size=1, queue="default,celery")
        with pytest.raises(KeyError):
            orch.recycle("not-a-real-id")


# ── reconcile ───────────────────────────────────────────────────────────


class TestReconcile:
    def test_recycles_when_jobs_done_exceeds_threshold(self, orch):
        orch.start_pool("fast", size=2, queue="default,celery")
        workers = list(orch._pools["fast"].values())
        workers[0].jobs_done = 10  # at threshold
        report = orch.reconcile()
        assert workers[0].worker_id in report["recycled"]

    def test_refills_pool_when_worker_dies(self, orch, client):
        orch.start_pool("fast", size=2, queue="default,celery")
        # Mark one worker dead; reconcile should respawn to size=2.
        first = list(orch._pools["fast"].values())[0]
        first.state = WorkerState.DEAD
        call_count_before = client.containers.run.call_count
        report = orch.reconcile()
        assert report["refilled"].get("fast") == 1
        assert client.containers.run.call_count == call_count_before + 1


# ── reap_orphans ────────────────────────────────────────────────────────


class TestReapOrphans:
    def test_kills_unknown_labelled_containers(self, orch, client):
        orch.start_pool("fast", size=1, queue="default,celery")
        tracked_id = list(orch._pools["fast"].values())[0].container_id

        orphan = _make_container("orphan-id", labels={LABEL_ROLE: ROLE_WORKER})
        # list(all=True, filters=...) gets called twice in this test path
        # (once during pool_status refresh, once in reap_orphans). Only the
        # reap call should see the orphan.
        client.containers.list.return_value = [
            _make_container(tracked_id, labels={LABEL_ROLE: ROLE_WORKER}),
            orphan,
        ]
        removed = orch.reap_orphans()
        assert "orphan-id" in removed
        orphan.remove.assert_called_once_with(force=True)


# ── terminate_pool ──────────────────────────────────────────────────────


class TestTerminatePool:
    def test_removes_every_worker(self, orch):
        orch.start_pool("fast", size=3, queue="default,celery")
        count = orch.terminate_pool("fast")
        assert count == 3
        assert orch._pools["fast"] == {}

    def test_unknown_pool_returns_zero(self, orch):
        assert orch.terminate_pool("nope") == 0


class TestAdoptExisting:
    """Exercises the config-hash gate on `_adopt_existing`.

    Compose-restart with a changed .env used to silently keep old workers
    running with stale settings. The orchestrator now stamps a hash of
    the current settings on each container at spawn-time and only adopts
    containers whose hash matches the current one.
    """

    def _make_worker_container(self, *, worker_id, pool="fast",
                               queue="default,celery", config_hash="abc123",
                               status="running"):
        from orchestrator.docker_sdk import (
            LABEL_CONFIG_HASH,
            LABEL_POOL,
            LABEL_QUEUE,
            LABEL_ROLE,
            LABEL_WORKER_ID,
            ROLE_WORKER,
        )
        return _make_container(
            container_id=f"cid-{worker_id}",
            status=status,
            labels={
                LABEL_ROLE: ROLE_WORKER,
                LABEL_POOL: pool,
                LABEL_QUEUE: queue,
                LABEL_WORKER_ID: worker_id,
                LABEL_CONFIG_HASH: config_hash,
            },
        )

    def test_adopts_when_hash_matches(self, orch, client, monkeypatch):
        """Pre-existing worker with the current config hash is re-attached."""
        from orchestrator import docker_sdk
        monkeypatch.setattr(docker_sdk, "_compute_config_hash", lambda: "match")
        pre = self._make_worker_container(worker_id="fast-old", config_hash="match")
        client.containers.list.return_value = [pre]
        orch.start_pool("fast", size=1, queue="default,celery")
        # Adopted, no fresh spawn.
        assert "fast-old" in orch._pools["fast"]
        assert client.containers.run.call_count == 0

    def test_tears_down_when_hash_mismatches(self, orch, client, monkeypatch):
        """Stale worker (different .env at original spawn) is recycled, not adopted.

        Note: _tear_down_container re-fetches the container via
        client.containers.get(cid), so .stop()/.remove() land on the
        fixture's fresh-mock-per-get instance, not on `stale` itself.
        We assert via client.containers.get's call list.
        """
        from orchestrator import docker_sdk
        monkeypatch.setattr(docker_sdk, "_compute_config_hash", lambda: "new")
        stale = self._make_worker_container(worker_id="fast-stale",
                                            config_hash="old")
        client.containers.list.return_value = [stale]
        get_calls_before = list(client.containers.get.call_args_list)
        orch.start_pool("fast", size=1, queue="default,celery")
        # Stale worker not adopted.
        assert "fast-stale" not in orch._pools["fast"]
        # _tear_down_container was called on the stale container's id.
        new_get_calls = client.containers.get.call_args_list[len(get_calls_before):]
        assert any(call.args == (stale.id,) for call in new_get_calls), (
            f"_tear_down_container should have re-fetched {stale.id} via "
            f"client.containers.get; got calls: {new_get_calls}"
        )
        # A fresh worker spawned to fill the pool.
        assert client.containers.run.call_count == 1

    def test_tears_down_when_label_missing(self, orch, client, monkeypatch):
        """Pre-feature workers have no config_hash label — treated as stale."""
        from orchestrator import docker_sdk
        monkeypatch.setattr(docker_sdk, "_compute_config_hash", lambda: "new")
        legacy = self._make_worker_container(worker_id="fast-legacy",
                                             config_hash="")
        # Strip the label entirely (simulate a worker spawned before this feature).
        from orchestrator.docker_sdk import LABEL_CONFIG_HASH
        del legacy.labels[LABEL_CONFIG_HASH]
        client.containers.list.return_value = [legacy]
        get_calls_before = list(client.containers.get.call_args_list)
        orch.start_pool("fast", size=1, queue="default,celery")
        assert "fast-legacy" not in orch._pools["fast"]
        new_get_calls = client.containers.get.call_args_list[len(get_calls_before):]
        assert any(call.args == (legacy.id,) for call in new_get_calls), (
            "_tear_down_container should have re-fetched the legacy container"
        )


class TestComputeConfigHash:
    def test_hash_is_stable_for_same_settings(self):
        from orchestrator.docker_sdk import _compute_config_hash
        h1 = _compute_config_hash()
        h2 = _compute_config_hash()
        assert h1 == h2
        assert len(h1) == 12
        assert all(c in "0123456789abcdef" for c in h1)

    def test_hash_changes_when_settings_change(self, monkeypatch):
        """Bumping any setting must change the hash so adopt-time
        comparison correctly flags stale workers."""
        from orchestrator.docker_sdk import _compute_config_hash
        from settings import settings as live
        original = _compute_config_hash()
        # Patch in-place via monkeypatch — covers any pydantic field.
        monkeypatch.setattr(live, "MAX_TRAJECTORY_FRAMES",
                            live.MAX_TRAJECTORY_FRAMES + 1)
        bumped = _compute_config_hash()
        assert bumped != original

    def test_spawn_stamps_current_hash_on_container(self, orch, client):
        from orchestrator.docker_sdk import (
            LABEL_CONFIG_HASH,
            _compute_config_hash,
        )
        orch.start_pool("fast", size=1, queue="default,celery")
        labels = client.containers.run.call_args.kwargs["labels"]
        assert labels[LABEL_CONFIG_HASH] == _compute_config_hash()
