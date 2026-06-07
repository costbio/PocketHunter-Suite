"""Orchestrator ABC + pool-state dataclasses.

Backends (``docker_sdk``, ``k8s``) implement :class:`Orchestrator`. The
entrypoint in ``orchestrator.__main__`` and the HTTP API in
``orchestrator.api`` consume the ABC, not the implementations — so a
single test double can substitute for the real Docker SDK in unit tests.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional


class WorkerState(str, Enum):
    """Coarse lifecycle states for a worker container.

    Strings rather than ints so the JSON the HTTP API emits stays readable.
    """

    STARTING = "starting"   # container created, Celery not yet ready
    READY = "ready"         # joined the broker, accepting jobs
    BUSY = "busy"           # at least one task in progress (best-effort)
    RECYCLING = "recycling" # tear-down in flight after jobs_done cap
    DEAD = "dead"           # exited or unresponsive; reconcile loop will replace


@dataclass
class WorkerInfo:
    """One worker's slice of pool state."""

    worker_id: str          # orchestrator-internal stable ID (uuid fragment)
    container_id: str       # docker container ID (or empty string if not yet up)
    pool: str               # "fast" | "docking"
    queue: str              # celery -Q value the worker is consuming
    jobs_done: int = 0      # count from celery inspect().stats()['total']
    state: WorkerState = WorkerState.STARTING
    image: str = ""
    started_at: Optional[float] = None  # unix ts; None until container is created

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d


@dataclass
class PoolStatus:
    """Snapshot of one pool's workers — returned by ``Orchestrator.pool_status``."""

    name: str                       # "fast" | "docking"
    queue: str                      # the celery -Q string this pool serves
    desired_size: int               # configured target (FAST_POOL_SIZE / DOCKING_POOL_SIZE)
    workers: list[WorkerInfo] = field(default_factory=list)

    @property
    def actual_size(self) -> int:
        return len([w for w in self.workers if w.state != WorkerState.DEAD])

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "queue": self.queue,
            "desired_size": self.desired_size,
            "actual_size": self.actual_size,
            "workers": [w.to_dict() for w in self.workers],
        }


class Orchestrator(ABC):
    """Backend-agnostic worker-pool manager.

    Concrete backends (Docker SDK over the socket proxy; k8s in a later
    phase) implement these methods. The entrypoint loop calls them on a
    timer; the HTTP API calls them on demand.
    """

    @abstractmethod
    def start_pool(self, name: str, size: int, queue: str) -> PoolStatus:
        """Create ``size`` workers for ``name`` consuming ``queue``.

        Idempotent: calling twice with the same name returns the existing
        pool rather than doubling its size.
        """

    @abstractmethod
    def pool_status(self, name: str) -> PoolStatus:
        """Return the current snapshot of pool ``name``.

        Raises :class:`KeyError` if the pool has never been started.
        """

    @abstractmethod
    def recycle(self, worker_id: str) -> WorkerInfo:
        """Tear down the named worker and spawn a fresh replacement.

        Returns the new ``WorkerInfo`` (same pool/queue, new container_id).
        Raises :class:`KeyError` if the worker isn't tracked.
        """

    @abstractmethod
    def reap_orphans(self) -> list[str]:
        """Kill any worker-labelled containers the orchestrator doesn't track.

        Returns the list of container IDs that were removed. A no-op when
        the orchestrator was just restarted and re-adopted everything.
        """

    @abstractmethod
    def terminate_pool(self, name: str) -> int:
        """Tear down every worker in pool ``name``.

        Returns the count removed. Used by graceful shutdown + tests.
        """

    @abstractmethod
    def reconcile(self) -> dict:
        """Single tick of the supervisor loop.

        Drives the recycle policy (jobs-done >= WORKER_JOBS_BEFORE_RECYCLE),
        refills pools that have lost workers, and reaps orphans. Returns a
        small report dict so the entrypoint loop can log meaningfully.
        """
