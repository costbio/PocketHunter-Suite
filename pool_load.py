"""Compute pool-load snapshots for the masthead load strip.

One public surface — ``compute_pool_load(pool)`` — returning a
``PoolLoad`` dataclass with busy/total/queued/estimated-wait. The
masthead's ``render_pool_load_strip()`` (in ``landing.py``) calls this
every 15 s via a fragment.

Stays DB-only: no orchestrator HTTP hop. Worker capacity comes from
``Config.FAST_POOL_SIZE`` / ``Config.DOCKING_POOL_SIZE`` (what users
actually experience); in-flight count + mean recent duration come from
``db.jobs``.
"""
from __future__ import annotations

from dataclasses import dataclass

from config import Config
from db import jobs as _jobs_repo


@dataclass(frozen=True)
class PoolLoad:
    pool: str               # "fast" | "docking"
    workers_busy: int       # in-flight jobs, capped at workers_total
    workers_total: int      # configured pool size
    queued: int             # in-flight beyond worker capacity
    est_wait_seconds: int   # 0 when nothing is queued
    avg_job_seconds: int    # 0 when no completed jobs in the lookback


def _pool_size(pool: str) -> int:
    if pool == "fast":
        return int(Config.FAST_POOL_SIZE)
    if pool == "docking":
        return int(Config.DOCKING_POOL_SIZE)
    raise ValueError(f"Unknown pool: {pool!r} (expected 'fast' or 'docking')")


def compute_pool_load(pool: str) -> PoolLoad:
    workers_total = _pool_size(pool)
    in_flight = _jobs_repo.in_flight_count(pool=pool)
    workers_busy = min(in_flight, workers_total)
    queued = max(0, in_flight - workers_total)
    avg = _jobs_repo.mean_recent_duration_seconds(pool=pool)
    est = (queued * avg) // workers_total if queued and avg and workers_total else 0
    return PoolLoad(
        pool=pool,
        workers_busy=workers_busy,
        workers_total=workers_total,
        queued=queued,
        est_wait_seconds=est,
        avg_job_seconds=avg,
    )


def format_wait_seconds(seconds: int) -> str:
    """Human-friendly compact form: '12s', '~3 min', '~1 h 20 min'."""
    if seconds <= 0:
        return ""
    if seconds < 90:
        return f"~{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"~{minutes} min"
    hours = minutes // 60
    rem = minutes % 60
    return f"~{hours} h {rem} min" if rem else f"~{hours} h"
