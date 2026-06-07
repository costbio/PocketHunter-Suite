"""Kubernetes backend — placeholder.

Phase C ships only the single-box Docker SDK backend. The k8s seam exists
so that scaling beyond one host (Phase D, if needed) can add a real
implementation without rewriting the entrypoint or HTTP API. Until then,
selecting ``ORCHESTRATOR_BACKEND=k8s`` raises a clear error at startup.
"""
from __future__ import annotations

from orchestrator.base import Orchestrator, PoolStatus, WorkerInfo


class K8sOrchestrator(Orchestrator):
    """Not implemented in Phase C — scaling beyond one box is a later phase."""

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "K8s backend is not implemented yet. Set ORCHESTRATOR_BACKEND=docker_sdk."
        )

    # The ABC requires concrete implementations of all abstract methods even
    # though __init__ refuses to run; defining stubs keeps ``Orchestrator``
    # subclass-able and the type checker happy.

    def start_pool(self, name: str, size: int, queue: str) -> PoolStatus:  # pragma: no cover
        raise NotImplementedError

    def pool_status(self, name: str) -> PoolStatus:  # pragma: no cover
        raise NotImplementedError

    def recycle(self, worker_id: str) -> WorkerInfo:  # pragma: no cover
        raise NotImplementedError

    def reap_orphans(self) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    def terminate_pool(self, name: str) -> int:  # pragma: no cover
        raise NotImplementedError

    def reconcile(self) -> dict:  # pragma: no cover
        raise NotImplementedError
