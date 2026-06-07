"""HTTP surface for the orchestrator.

A deliberately tiny Flask app — three endpoints, no auth (the service
binds to the internal Docker network, never to the host). Used by:

  * Streamlit panels in C5 to render "waiting for a fast/docking slot"
    captions with real per-pool depth instead of legacy Redis polling.
  * The reverse proxy / monitoring stack for liveness via ``/healthz``.
  * Operators for ad-hoc inspection (``curl orchestrator:9000/pool/status``).
"""
from __future__ import annotations

import logging

from flask import Flask, abort, jsonify, request

from orchestrator.base import Orchestrator

log = logging.getLogger(__name__)


def _check_docker(orch: Orchestrator) -> dict:
    """Liveness probe for the Docker daemon (via the socket proxy)."""
    client = getattr(orch, "_client", None)
    if client is None:
        return {"ok": True, "detail": "backend has no docker client"}
    try:
        client.ping()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _check_redis() -> dict:
    """PING the Celery broker (which is also our Redis)."""
    try:
        import redis as _redis
        from settings import settings

        r = _redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=2)
        r.ping()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _check_db() -> dict:
    """SELECT 1 against Postgres."""
    try:
        from db.session import get_engine
        from sqlalchemy import text

        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _check_pools(orch: Orchestrator, pool_names: list[str]) -> dict:
    """Per-pool desired vs actual occupancy."""
    out: dict = {"ok": True, "detail": {}}
    for name in pool_names:
        try:
            s = orch.pool_status(name)
            out["detail"][name] = {
                "desired": s.desired_size,
                "actual": s.actual_size,
            }
            # A pool that's failing to refill is still "ok" for the
            # liveness probe — the reconcile loop will keep trying.
            # We only flip ``ok`` to False if pool_status() crashes.
        except KeyError:
            out["detail"][name] = {"desired": 0, "actual": 0, "started": False}
        except Exception as e:
            out["ok"] = False
            out["detail"][name] = {"error": f"{type(e).__name__}: {e}"}
    return out


def create_app(orch: Orchestrator, pool_names: list[str]) -> Flask:
    """Build the Flask app bound to a specific orchestrator instance.

    ``pool_names`` is the canonical list (``["fast", "docking"]`` in
    production) — ``/pool/status`` iterates this rather than relying on
    private state, so the JSON shape is stable even when a pool has 0
    workers (``USE_ORCHESTRATOR=false`` case).
    """
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz():
        """Phase C C5: deep health check for the reverse proxy / monitoring.

        Probes each runtime dependency individually so a 503 response
        carries enough info to point at the broken component without
        opening a shell. Returns 200 + ``checks: {…}`` when every
        component is reachable, 503 + the same body otherwise.
        """
        checks = {
            "docker": _check_docker(orch),
            "redis": _check_redis(),
            "db": _check_db(),
            "pools": _check_pools(orch, pool_names),
        }
        all_ok = all(v.get("ok") for v in checks.values())
        body = {"status": "ok" if all_ok else "unhealthy", "checks": checks}
        return jsonify(body), (200 if all_ok else 503)

    @app.get("/pool/status")
    def pool_status():
        out = {}
        for name in pool_names:
            try:
                out[name] = orch.pool_status(name).to_dict()
            except KeyError:
                # Surface the "not started" state explicitly rather than
                # 404'ing — clients (Streamlit panels) want a flat shape.
                out[name] = {
                    "name": name,
                    "desired_size": 0,
                    "actual_size": 0,
                    "workers": [],
                    "queue": "",
                    "started": False,
                }
        return jsonify(out)

    @app.post("/pool/recycle/<worker_id>")
    def pool_recycle(worker_id):
        try:
            info = orch.recycle(worker_id)
        except KeyError:
            abort(404, description=f"unknown worker: {worker_id}")
        return jsonify({"recycled": worker_id, "replacement": info.to_dict()})

    return app
