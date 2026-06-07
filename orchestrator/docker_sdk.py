"""Docker SDK orchestrator backend.

Single-box implementation: talks to ``docker-socket-proxy`` over a TCP
URL; spawns hardened worker containers with the flag set the C0 spike
validated end-to-end on 2026-05-15.

**The orchestrator never holds /var/run/docker.sock directly.** The proxy
is the choke point — its env-var allowlist (``CONTAINERS=1``, ``POST=1``,
``NETWORKS=1``, …) determines what API calls are reachable, so even a
compromised orchestrator container can't escape the docker.sock blast
radius.
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

import docker
from docker.errors import APIError, NotFound

from orchestrator.base import (
    Orchestrator,
    PoolStatus,
    WorkerInfo,
    WorkerState,
)

log = logging.getLogger(__name__)

# Labels are how the orchestrator finds *its* containers after a restart
# and how the reaper distinguishes orphans from third-party containers.
LABEL_ROLE = "pockethunter.role"
LABEL_POOL = "pockethunter.pool"
LABEL_WORKER_ID = "pockethunter.worker_id"
LABEL_QUEUE = "pockethunter.queue"
# Hash of the settings the worker would have read at import time. When
# .env changes and compose restarts, the orchestrator re-adopts pre-
# existing workers — but their celery process holds the OLD settings
# in memory from its original import. Comparing this label against the
# current hash on adopt lets us recycle stale workers automatically.
LABEL_CONFIG_HASH = "pockethunter.config_hash"
ROLE_WORKER = "worker"


def _compute_config_hash() -> str:
    """Stable 12-char hex digest of the current settings.

    Covers every field on the Settings class. Truncated to 12 hex
    (48 bits ≈ negligible collision risk for the handful of "worker
    generations" we ever spawn). Sensitive fields (DATABASE_URL,
    POSTGRES_PASSWORD, etc.) are mixed in but only their digest is
    stamped on the label; the raw values never leak.

    Canonicalisation: the raw ``model_dump_json()`` is non-deterministic
    across processes because some fields are ``set``-backed (or were
    coerced from sets) and Python's hash randomisation changes set
    iteration order per process. We re-parse the dump, recursively
    sort any list of comparable elements, and re-serialise with sorted
    keys before hashing — so two processes with the same .env produce
    the same hash, which is what the worker-adopt comparison requires.
    """
    import json

    from settings import settings as _settings  # lazy: orchestrator tests stub

    def _canonicalise(obj):
        if isinstance(obj, list):
            sorted_items = [_canonicalise(x) for x in obj]
            try:
                sorted_items.sort(key=lambda v: (type(v).__name__, v))
            except TypeError:
                pass  # heterogeneous list — leave order alone
            return sorted_items
        if isinstance(obj, dict):
            return {k: _canonicalise(v) for k, v in obj.items()}
        return obj

    canonical = _canonicalise(json.loads(_settings.model_dump_json()))
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _nano_cpus(cpus: int) -> int:
    """Convert whole-CPU count to Docker's nano_cpus units."""
    return int(cpus) * 1_000_000_000


class DockerSdkOrchestrator(Orchestrator):
    """Spawn + recycle worker containers over a Docker socket proxy."""

    def __init__(
        self,
        *,
        docker_url: str,
        worker_image: str,
        worker_cpu_limit: int,
        worker_memory_limit: str,
        worker_jobs_before_recycle: int,
        worker_drain_timeout: int,
        worker_network: str,
        host_repo_path: str,
        host_uploads_path: str,
        host_results_path: str,
        host_logs_path: str,
        celery_broker_url: str,
        celery_result_backend: str,
        database_url: str,
        base_url: str,
        client: Optional[docker.DockerClient] = None,
    ) -> None:
        self.docker_url = docker_url
        self.worker_image = worker_image
        self.worker_cpu_limit = worker_cpu_limit
        self.worker_memory_limit = worker_memory_limit
        self.worker_jobs_before_recycle = worker_jobs_before_recycle
        self.worker_drain_timeout = worker_drain_timeout
        self.worker_network = worker_network
        # Host-side absolute paths for the bind mounts — workers need the
        # *host* path, not the orchestrator's view, because the daemon
        # resolves volume sources on the host.
        self.host_repo_path = host_repo_path
        self.host_uploads_path = host_uploads_path
        self.host_results_path = host_results_path
        self.host_logs_path = host_logs_path
        # Celery + DB env propagated into each worker.
        self.celery_broker_url = celery_broker_url
        self.celery_result_backend = celery_result_backend
        self.database_url = database_url
        self.base_url = base_url

        self._client = client or docker.DockerClient(base_url=docker_url)
        # pool_name -> {worker_id: WorkerInfo}
        self._pools: dict[str, dict[str, WorkerInfo]] = {}
        # pool_name -> (size, queue) so we can refill after deaths
        self._pool_spec: dict[str, tuple[int, str]] = {}

    # ── Public API ─────────────────────────────────────────────────────

    def start_pool(self, name: str, size: int, queue: str) -> PoolStatus:
        if name in self._pools:
            log.info("pool %s already started; returning existing status", name)
            return self.pool_status(name)

        self._pools[name] = {}
        self._pool_spec[name] = (size, queue)

        # Adopt any pre-existing containers labelled for this pool (orchestrator
        # restart case) before deciding how many fresh ones to spawn.
        self._adopt_existing(name, queue)

        missing = size - len(self._pools[name])
        for _ in range(max(0, missing)):
            self._spawn_worker(name, queue)

        return self.pool_status(name)

    def pool_status(self, name: str) -> PoolStatus:
        if name not in self._pool_spec:
            raise KeyError(f"Unknown pool: {name!r}")
        size, queue = self._pool_spec[name]
        # Refresh state from Docker before reporting.
        self._refresh_states(name)
        return PoolStatus(
            name=name,
            queue=queue,
            desired_size=size,
            workers=list(self._pools[name].values()),
        )

    def recycle(self, worker_id: str) -> WorkerInfo:
        pool, info = self._find_worker(worker_id)
        log.info("recycling worker %s (pool=%s, jobs_done=%d)",
                 worker_id, pool, info.jobs_done)
        info.state = WorkerState.RECYCLING
        self._tear_down_container(info.container_id)
        del self._pools[pool][worker_id]
        _, queue = self._pool_spec[pool]
        return self._spawn_worker(pool, queue)

    def reap_orphans(self) -> list[str]:
        """Find worker-labelled containers we don't track and remove them."""
        tracked = {
            info.container_id
            for workers in self._pools.values()
            for info in workers.values()
            if info.container_id
        }
        orphans: list[str] = []
        for c in self._client.containers.list(
            all=True,
            filters={"label": f"{LABEL_ROLE}={ROLE_WORKER}"},
        ):
            if c.id not in tracked:
                log.warning("reaping orphan worker container %s", c.id[:12])
                try:
                    c.remove(force=True)
                    orphans.append(c.id)
                except APIError as e:
                    log.error("failed to remove orphan %s: %s", c.id[:12], e)
        return orphans

    def terminate_pool(self, name: str) -> int:
        if name not in self._pools:
            return 0
        count = 0
        for worker_id, info in list(self._pools[name].items()):
            try:
                self._tear_down_container(info.container_id)
                count += 1
            except Exception as e:  # pragma: no cover - best-effort shutdown
                log.error("teardown of %s failed: %s", worker_id, e)
            del self._pools[name][worker_id]
        return count

    def reconcile(self) -> dict:
        """One supervisor tick.

        - Refresh each worker's state from Docker.
        - Recycle workers past the jobs-done threshold.
        - Refill pools that have lost workers below their desired size.
        - Reap orphan containers.

        Returns a small report dict for structured logging.
        """
        report = {"recycled": [], "refilled": {}, "orphans": []}
        for name in list(self._pools.keys()):
            self._refresh_states(name)
            size, queue = self._pool_spec[name]

            for worker_id, info in list(self._pools[name].items()):
                if info.state == WorkerState.DEAD:
                    log.warning("worker %s is dead; replacing", worker_id)
                    del self._pools[name][worker_id]
                    continue
                if info.jobs_done >= self.worker_jobs_before_recycle:
                    self.recycle(worker_id)
                    report["recycled"].append(worker_id)

            shortfall = size - len(self._pools[name])
            for _ in range(max(0, shortfall)):
                self._spawn_worker(name, queue)
            if shortfall > 0:
                report["refilled"][name] = shortfall

        report["orphans"] = self.reap_orphans()
        return report

    # ── Internals ──────────────────────────────────────────────────────

    def _spawn_worker(self, pool: str, queue: str) -> WorkerInfo:
        worker_id = f"{pool}-{uuid.uuid4().hex[:8]}"
        container_name = f"ph-worker-{worker_id}"
        info = WorkerInfo(
            worker_id=worker_id,
            container_id="",
            pool=pool,
            queue=queue,
            state=WorkerState.STARTING,
            image=self.worker_image,
            started_at=time.time(),
        )
        self._pools[pool][worker_id] = info

        command = [
            "celery", "-A", "celery_app", "worker",
            "-Q", queue,
            "--concurrency=1",
            "-n", f"{worker_id}@%h",
            "--loglevel=info",
        ]
        env = {
            "CELERY_BROKER_URL": self.celery_broker_url,
            "CELERY_RESULT_BACKEND": self.celery_result_backend,
            "DATABASE_URL": self.database_url,
            "BASE_URL": self.base_url,
            "PYTHONPATH": "/app",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": "/tmp",
            "MPLCONFIGDIR": "/tmp/matplotlib",
            "XDG_CACHE_HOME": "/tmp/cache",
            "XDG_CONFIG_HOME": "/tmp/config",
            "WORKER_ID": worker_id,
            "WORKER_POOL": pool,
            # /app is mounted RO; the logger needs to write somewhere on
            # one of the writable bind mounts.
            "LOG_FILE": "/app/logs/pockethunter-suite.log",
        }
        labels = {
            LABEL_ROLE: ROLE_WORKER,
            LABEL_POOL: pool,
            LABEL_WORKER_ID: worker_id,
            LABEL_QUEUE: queue,
            LABEL_CONFIG_HASH: _compute_config_hash(),
        }
        volumes = {
            # Source tree read-only — image already has it COPYed at build,
            # but bind-mounting matches the dev workflow (hot reload of code).
            # ``--read-only`` rootfs + this RO mount means workers cannot
            # tamper with the suite code.
            self.host_repo_path: {"bind": "/app", "mode": "ro"},
            self.host_uploads_path: {"bind": "/app/uploads", "mode": "ro"},
            self.host_results_path: {"bind": "/app/results", "mode": "rw"},
            self.host_logs_path: {"bind": "/app/logs", "mode": "rw"},
        }

        try:
            container = self._client.containers.run(
                image=self.worker_image,
                command=command,
                name=container_name,
                detach=True,
                environment=env,
                labels=labels,
                volumes=volumes,
                network=self.worker_network,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                user="1000:1000",
                tmpfs={
                    "/tmp": "exec,size=1g",
                    "/scratch": "size=4g",
                },
                mem_limit=self.worker_memory_limit,
                nano_cpus=_nano_cpus(self.worker_cpu_limit),
                init=True,
                restart_policy={"Name": "no"},
                working_dir="/app",
            )
            info.container_id = container.id
            log.info("spawned worker %s as container %s", worker_id, container.id[:12])
        except APIError as e:
            info.state = WorkerState.DEAD
            log.error("failed to spawn worker %s: %s", worker_id, e)
        return info

    def _adopt_existing(self, pool: str, queue: str) -> None:
        """Re-attach to containers labelled for this pool after a restart.

        Containers whose ``pockethunter.config_hash`` label doesn't match
        the *current* settings hash are NOT adopted — they get torn down
        instead. This is what makes ``docker compose down && up`` actually
        propagate .env changes: the new orchestrator detects the stale
        workers and respawns them with the fresh settings imported. Old
        workers without the label at all (pre-this-feature) are also
        treated as stale on first orchestrator restart after upgrade.
        """
        current_hash = _compute_config_hash()
        for c in self._client.containers.list(
            all=True,
            filters={"label": [f"{LABEL_ROLE}={ROLE_WORKER}", f"{LABEL_POOL}={pool}"]},
        ):
            worker_id = c.labels.get(LABEL_WORKER_ID)
            if not worker_id or worker_id in self._pools[pool]:
                continue
            container_hash = c.labels.get(LABEL_CONFIG_HASH, "")
            if container_hash != current_hash:
                log.info(
                    "worker %s has stale config_hash=%s (current=%s); "
                    "tearing down so a fresh worker can spawn with the new settings",
                    worker_id, container_hash or "(unset)", current_hash,
                )
                self._tear_down_container(c.id)
                continue
            self._pools[pool][worker_id] = WorkerInfo(
                worker_id=worker_id,
                container_id=c.id,
                pool=pool,
                queue=c.labels.get(LABEL_QUEUE, queue),
                state=(
                    WorkerState.READY if c.status == "running" else WorkerState.DEAD
                ),
                image=self.worker_image,
                started_at=None,
            )
            log.info("adopted existing worker %s (%s)", worker_id, c.status)

    def _refresh_states(self, pool: str) -> None:
        for info in self._pools[pool].values():
            if not info.container_id:
                continue
            try:
                c = self._client.containers.get(info.container_id)
            except NotFound:
                info.state = WorkerState.DEAD
                continue
            if c.status != "running":
                info.state = WorkerState.DEAD
            elif info.state == WorkerState.STARTING:
                # Trust Docker for liveness; Celery readiness check is the
                # job of a future C5 improvement (probe inspect().ping()).
                info.state = WorkerState.READY

    def _tear_down_container(self, container_id: str) -> None:
        """Stop + remove a worker, draining any in-flight task first.

        docker stop sends SIGTERM and waits up to ``worker_drain_timeout``
        seconds before SIGKILL. Celery's default signal handling on SIGTERM
        is a warm shutdown: the worker stops accepting new tasks and exits
        cleanly after the current one finishes. This keeps a recycle from
        SIGKILLing an in-flight docking pair, which used to cause the
        symptom 'browser-close = docking aborts'. Combined with celery's
        task_acks_late + task_reject_on_worker_lost (celery_app.py), even
        an over-the-drain-timeout task that ends in SIGKILL gets requeued
        on a fresh worker, which then resumes from the partial CSV.
        """
        if not container_id:
            return
        try:
            c = self._client.containers.get(container_id)
            try:
                c.stop(timeout=self.worker_drain_timeout)
                c.remove()
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "graceful stop of %s failed (%s); force-removing",
                    container_id[:12], e,
                )
                c.remove(force=True)
        except NotFound:
            pass

    def _find_worker(self, worker_id: str) -> tuple[str, WorkerInfo]:
        for pool, workers in self._pools.items():
            if worker_id in workers:
                return pool, workers[worker_id]
        raise KeyError(f"Unknown worker: {worker_id!r}")


def build_from_settings() -> DockerSdkOrchestrator:
    """Construct an orchestrator from the project's ``settings`` singleton.

    Kept separate from ``__init__`` so unit tests can build instances with
    explicit kwargs and a mock client.
    """
    from settings import settings

    # Host paths — the orchestrator runs *in* a container too, so we need
    # to know what path the daemon will resolve volume sources against.
    # The compose file passes these in via env vars.
    import os
    host_repo = os.environ.get("HOST_REPO_PATH", str(Path.cwd()))
    return DockerSdkOrchestrator(
        docker_url=settings.DOCKER_PROXY_URL,
        worker_image=settings.WORKER_IMAGE_TAG,
        worker_cpu_limit=settings.WORKER_CPU_LIMIT,
        worker_memory_limit=settings.WORKER_MEMORY_LIMIT,
        worker_jobs_before_recycle=settings.WORKER_JOBS_BEFORE_RECYCLE,
        worker_drain_timeout=settings.WORKER_DRAIN_TIMEOUT,
        worker_network=settings.WORKER_NETWORK_NAME,
        host_repo_path=host_repo,
        host_uploads_path=os.environ.get("HOST_UPLOADS_PATH", f"{host_repo}/uploads"),
        host_results_path=os.environ.get("HOST_RESULTS_PATH", f"{host_repo}/results"),
        host_logs_path=os.environ.get("HOST_LOGS_PATH", f"{host_repo}/logs"),
        celery_broker_url=settings.CELERY_BROKER_URL,
        celery_result_backend=settings.CELERY_RESULT_BACKEND,
        database_url=settings.DATABASE_URL,
        base_url=settings.BASE_URL,
    )
