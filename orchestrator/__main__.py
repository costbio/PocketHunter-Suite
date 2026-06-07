"""Orchestrator process entrypoint.

Runs in its own container alongside Streamlit + Postgres + Redis. On
start: instantiates the chosen backend, optionally spawns the two pools,
serves the HTTP API on ``ORCHESTRATOR_HTTP_PORT``, and reconciles every
``ORCHESTRATOR_TICK_SECONDS``.

The reconcile loop is the *only* writer to the worker pools — the HTTP
API only reads state and routes manual recycle requests through the same
orchestrator instance, so there are no concurrent-mutation races.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
import time

from orchestrator.api import create_app
from orchestrator.base import Orchestrator

POOL_NAMES = ["fast", "docking"]
POOL_QUEUES = {"fast": "default,celery", "docking": "docking"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("orchestrator")


def _build_orchestrator() -> Orchestrator:
    """Pick the backend the user asked for, or fail loudly."""
    from settings import settings

    backend = settings.ORCHESTRATOR_BACKEND.lower()
    if backend == "docker_sdk":
        from orchestrator.docker_sdk import build_from_settings
        return build_from_settings()
    if backend == "k8s":
        from orchestrator.k8s import K8sOrchestrator
        return K8sOrchestrator()
    raise ValueError(f"Unknown ORCHESTRATOR_BACKEND: {backend!r}")


def _start_pools_if_enabled(orch: Orchestrator) -> None:
    from settings import settings

    if not settings.USE_ORCHESTRATOR:
        log.info("USE_ORCHESTRATOR=false → spawning zero workers; legacy "
                 "celery-worker / celery-docking-worker services own the queues")
        return
    log.info("USE_ORCHESTRATOR=true → starting fast (%d) + docking (%d) pools",
             settings.FAST_POOL_SIZE, settings.DOCKING_POOL_SIZE)
    orch.start_pool("fast", settings.FAST_POOL_SIZE, POOL_QUEUES["fast"])
    orch.start_pool("docking", settings.DOCKING_POOL_SIZE, POOL_QUEUES["docking"])


def _reconcile_loop(orch: Orchestrator, stop: threading.Event) -> None:
    from settings import settings

    period = settings.ORCHESTRATOR_TICK_SECONDS
    log.info("reconcile loop starting (period=%ds)", period)
    while not stop.wait(period):
        try:
            report = orch.reconcile()
            if any(report.values()):
                log.info("reconcile: %s", report)
        except Exception as e:  # pragma: no cover - keep the loop alive
            log.exception("reconcile tick failed: %s", e)
    log.info("reconcile loop exiting")


def main() -> int:
    from settings import settings

    log.info("orchestrator starting (backend=%s, http_port=%d)",
             settings.ORCHESTRATOR_BACKEND, settings.ORCHESTRATOR_HTTP_PORT)

    orch = _build_orchestrator()
    _start_pools_if_enabled(orch)

    stop = threading.Event()
    loop_thread = threading.Thread(
        target=_reconcile_loop, args=(orch, stop), daemon=True, name="reconcile",
    )
    loop_thread.start()

    def _shutdown(signum, frame):
        log.info("signal %d received → stopping", signum)
        stop.set()
        # Best-effort drain: leave existing workers in place. C3+ adds
        # `terminate_pool` on shutdown when ownership is fully transferred.
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Run Flask in the main thread (so signals reach it). Werkzeug's dev
    # server is fine for an internal-only API; C5 can swap to gunicorn
    # if pool sizes grow.
    app = create_app(orch, POOL_NAMES)
    app.run(host="0.0.0.0", port=settings.ORCHESTRATOR_HTTP_PORT, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
