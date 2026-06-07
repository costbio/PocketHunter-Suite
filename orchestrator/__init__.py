"""Phase C orchestrator — bounded, recycled worker pool manager.

The orchestrator owns the lifecycle of hardened worker containers.
Streamlit dispatches jobs to Redis (broker) as before; the orchestrator
watches Celery's ``inspect`` and the Docker daemon (via the socket proxy)
to keep ``FAST_POOL_SIZE`` + ``DOCKING_POOL_SIZE`` workers alive,
recycling them after every ``WORKER_JOBS_BEFORE_RECYCLE`` jobs.

Two pools — preserved from the B-era two-queue design — so a flood of
slow docking jobs cannot starve quick pipeline jobs.

Backends live in sibling modules (``docker_sdk``, ``k8s``); see
``base.Orchestrator`` for the contract they implement.
"""

from orchestrator.base import (  # noqa: F401
    Orchestrator,
    PoolStatus,
    WorkerInfo,
    WorkerState,
)
