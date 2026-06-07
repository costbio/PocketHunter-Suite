"""Helpers shared by the Pockets / Cluster / Dock panels.

Three states each panel walks through — settings → running → results —
share a small set of primitives: looking up the latest completed Job of a
given ``kind`` from the DB, detecting whether a pipeline run-all task is
currently in flight, rendering the polling progress bar + sleep+rerun
loop, and rendering structured failures. Centralised here so the three
panel modules stay focused on their own forms + results UI.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import streamlit as st


def new_job_id(prefix: str) -> str:
    """Build a fresh ``<prefix>_<YYYYMMDD_HHMMSS>_<uuid8>`` identifier.

    Matches the existing on-disk job_id convention used by the legacy
    pages (timestamp + uuid fragment). Stable across panels so the disk
    layout stays uniform.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    return f"{prefix}_{ts}_{suffix}"


@st.cache_data(show_spinner=False)
def _frame_index_map(results_job_id: str) -> dict[str, int]:
    """Cache-backed map of ``per-frame PDB filename → 1-based model index``.

    Uses :func:`viewer_pipeline.sorted_frame_pdbs` for ordering so this
    matches the multi-model PDB ``convert_pdb_dir_to_viewer`` writes:
    numerical sort by the trailing ``_<N>.pdb`` index. Lexicographic
    sort would put ``_fit_10.pdb`` before ``_fit_2.pdb`` and break the
    "click-a-pocket-row jumps to its source frame" mapping.

    Cached because the listing doesn't change once the find_pockets
    task is complete; cleared by ``cleanup_job`` when the per-session
    results dir is pruned.
    """
    from config import Config
    from viewer_pipeline import sorted_frame_pdbs

    pdb_dir = Path(Config.RESULTS_DIR) / results_job_id / "pdbs"
    if not pdb_dir.exists():
        return {}
    return {p.name: i for i, p in enumerate(sorted_frame_pdbs(pdb_dir), start=1)}


def _normalise_pdb_filename(file_name: str) -> str:
    """Strip p2rank's ``_predictions`` suffix and ensure ``.pdb`` tail.

    pockethunter's ``pockets.csv`` carries the file name with a
    ``_predictions`` suffix appended by p2rank (e.g.
    ``..._fit_84.pdb_predictions``).
    """
    name = str(file_name).strip()
    if name.endswith("_predictions"):
        name = name[: -len("_predictions")]
    if not name.endswith(".pdb"):
        name = f"{name}.pdb"
    return name


def frame_index_for_filename(results_job_id: str, file_name: str) -> Optional[int]:
    """Resolve the 1-based *dense* (pre-stride) model index.

    Kept for legacy call sites; prefer :func:`viewer_target_for_filename`
    for new code — it surfaces whether the frame is baked into the
    strided ``viewer.pdb`` or needs an on-demand single-frame load.

    Returns ``None`` when the file isn't present in the job's pdbs dir
    (e.g. pruned, renamed, or a typo in pockets.csv).
    """
    if not results_job_id or not file_name:
        return None
    return _frame_index_map(results_job_id).get(_normalise_pdb_filename(file_name))


@dataclass(frozen=True)
class ViewerFrameTarget:
    """Resolved frame target for the viewer fragment.

    ``filename`` is the canonical per-frame PDB name (post-normalisation).
    ``logical_index`` is the 1-based dense index across the full extracted
    set (used for the focused-mode "Frame N of M" label).
    ``viewer_model`` is the 1-based model index in the strided
    ``viewer.pdb`` when the frame is in the strided set, or ``None`` when
    it isn't — the viewer fragment swaps to single-frame mode in that case.
    """

    filename: str
    logical_index: int
    viewer_model: Optional[int]


@st.cache_data(show_spinner=False)
def _viewer_manifest(results_job_id: str, mtime_ns: int) -> Optional[dict]:
    """Read ``viewer_index.json`` next to ``viewer.pdb``.

    ``mtime_ns`` is part of the cache key so a worker rebuild invalidates
    the cached payload (next call passes a fresh mtime). Returns ``None``
    when the manifest is missing or unreadable.
    """
    from config import Config

    manifest_path = (
        Path(Config.RESULTS_DIR) / results_job_id / "viewer_index.json"
    )
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return None


def _manifest_mtime_ns(results_job_id: str) -> int:
    """Cache-key helper — 0 when the manifest doesn't exist yet."""
    from config import Config

    manifest_path = (
        Path(Config.RESULTS_DIR) / results_job_id / "viewer_index.json"
    )
    try:
        return manifest_path.stat().st_mtime_ns
    except OSError:
        return 0


def viewer_target_for_filename(
    results_job_id: str, file_name: str
) -> Optional[ViewerFrameTarget]:
    """Resolve a per-frame PDB filename → :class:`ViewerFrameTarget`.

    Uses the on-disk stride manifest (``viewer_index.json``) when present;
    falls back to the dense-index map otherwise (so callers handle a
    legacy session — built before the stride feature — without crashing).

    Returns ``None`` when the file isn't known to either source.
    """
    if not results_job_id or not file_name:
        return None
    name = _normalise_pdb_filename(file_name)
    dense_map = _frame_index_map(results_job_id)
    logical = dense_map.get(name)
    if logical is None:
        return None

    manifest = _viewer_manifest(results_job_id, _manifest_mtime_ns(results_job_id))
    if manifest is None:
        # Legacy session — no manifest on disk. Treat as stride=1: every
        # logical frame is also a viewer model at the same index.
        return ViewerFrameTarget(
            filename=name, logical_index=int(logical), viewer_model=int(logical)
        )

    logical_to_viewer = manifest.get("logical_to_viewer") or {}
    viewer_model = logical_to_viewer.get(name)
    return ViewerFrameTarget(
        filename=name,
        logical_index=int(logical),
        viewer_model=int(viewer_model) if viewer_model is not None else None,
    )


def latest_job_of_kind(
    session_id,
    kinds: Iterable[str],
) -> Optional[dict]:
    """Return the most-recently-updated Job dict whose ``kind`` is in ``kinds``.

    Wraps :func:`failure_view.load_status_for_session` (which already
    returns DB rows as dicts in the same shape the disk status-JSON files
    used) and filters in Python. Reading all jobs for a session is cheap;
    if the volume ever bites we can push the filter into the DB query.

    Returns ``None`` when the session has no matching jobs.
    """
    if session_id is None:
        return None
    from failure_view import load_status_for_session

    kinds = tuple(kinds)
    matches = [
        row
        for row in load_status_for_session(session_id)
        if row.get("kind") in kinds
    ]
    if not matches:
        return None
    # ``last_updated`` is an ISO string (lexicographically sortable). Rows
    # without it sort to "oldest" so a row with a real timestamp wins.
    return max(matches, key=lambda r: r.get("last_updated") or "")


def jobs_of_kind(
    session_id,
    kinds: Iterable[str],
) -> list[dict]:
    """Return all Job dicts whose ``kind`` is in ``kinds``, newest-first.

    Same source as :func:`latest_job_of_kind` — ``load_status_for_session``
    — but the full ordered list, for run-history dropdowns.
    """
    if session_id is None:
        return []
    from failure_view import load_status_for_session

    kinds = tuple(kinds)
    matches = [
        row
        for row in load_status_for_session(session_id)
        if row.get("kind") in kinds
    ]
    matches.sort(key=lambda r: r.get("last_updated") or "", reverse=True)
    return matches


def job_by_legacy_id(session_id, legacy_id: str) -> Optional[dict]:
    """Return the session's Job dict with the given ``legacy_id``.

    ``None`` when the session has no such job (e.g. a stale
    ``*_view_job_id`` override pointing at a pruned run — callers fall
    back to :func:`latest_job_of_kind`).
    """
    if session_id is None or not legacy_id:
        return None
    from failure_view import load_status_for_session

    for row in load_status_for_session(session_id):
        if row.get("legacy_id") == legacy_id:
            return row
    return None


_RUNNING_STATES = ("PENDING", "PROGRESS", "RECEIVED", "STARTED", "RETRY")


def pipeline_in_flight() -> bool:
    """Back-compat shim — the chained pipeline task was removed (B2.1).

    Always returns False. Kept as a name so any panel still importing it
    via ``from panels._shared import pipeline_in_flight`` keeps loading
    while the call sites are scrubbed. Delete this once nothing imports
    it.
    """
    return False


def any_panel_task_running() -> bool:
    """``True`` when any tracked panel task is live.

    Used by the Run-all button to refuse a second submission while the
    session already has something on the queue. Cheap — at most three
    AsyncResult lookups against Redis.
    """
    keys = (
        "find_pockets_task_id",
        "cluster_task_id",
        "docking_task_id",
    )
    for k in keys:
        task_id = st.session_state.get(k)
        if not task_id:
            continue
        task = get_async_result(task_id)
        if task is not None and task.state in _RUNNING_STATES:
            return True
    return False


def get_async_result(task_id: str):
    """Tiny wrapper so panel modules don't all duplicate the import.

    Returns ``None`` on import failure (no Celery in the test environment),
    letting the caller fall through to "no in-flight task" state instead
    of crashing the page.
    """
    try:
        from celery_app import celery_app

        return celery_app.AsyncResult(task_id)
    except Exception:
        return None


class PoolCapHit(Exception):
    """Phase C C4: a submit-time pool cap was reached.

    Two variants encoded in ``scope``:

    * ``"global"`` — the pool itself is at ``MAX_CONCURRENT_*_JOBS``;
      every session is blocked from submitting here until something
      finishes.
    * ``"session"`` — *this* session has already
      ``MAX_CONCURRENT_*_PER_SESSION`` in flight; other sessions can
      still submit. Prevents one user from monopolising a pool.
    """

    def __init__(self, *, pool: str, scope: str, current: int, cap: int):
        self.pool = pool
        self.scope = scope
        self.current = current
        self.cap = cap
        super().__init__(f"{pool} pool {scope} cap reached: {current}/{cap} in flight")


def assert_submit_allowed(pool: str, session_id: Optional[uuid.UUID]) -> None:
    """Refuse a submission when the pool is at its global or per-session cap.

    Called from panels' submit paths immediately before ``.delay()``.
    Honours ``Config.RATE_LIMIT_ENABLED`` — when disabled, this is a
    no-op so local dev isn't constrained.

    Raises :class:`PoolCapHit` so the panel can render an ``st.error``
    with specific scope/cap details and skip the Celery call.
    """
    from config import Config
    from db import jobs as _jobs_repo

    if not Config.RATE_LIMIT_ENABLED:
        return
    if pool == "fast":
        global_cap = Config.MAX_CONCURRENT_FAST_JOBS
        per_session_cap = Config.MAX_CONCURRENT_FAST_PER_SESSION
    elif pool == "docking":
        global_cap = Config.MAX_CONCURRENT_DOCKING_JOBS
        per_session_cap = Config.MAX_CONCURRENT_DOCKING_PER_SESSION
    else:
        raise ValueError(f"Unknown pool: {pool!r}")

    try:
        global_count = _jobs_repo.in_flight_count(pool=pool)
    except Exception:
        global_count = 0  # fail-open on DB hiccup
    if global_count >= global_cap:
        raise PoolCapHit(pool=pool, scope="global",
                         current=global_count, cap=global_cap)

    if session_id is not None:
        try:
            session_count = _jobs_repo.in_flight_count(pool=pool, session_id=session_id)
        except Exception:
            session_count = 0
        if session_count >= per_session_cap:
            raise PoolCapHit(pool=pool, scope="session",
                             current=session_count, cap=per_session_cap)


def render_pool_cap_error(e: PoolCapHit) -> None:
    """Standard panel-side renderer for a :class:`PoolCapHit`."""
    if e.scope == "session":
        st.error(
            f"You already have {e.current}/{e.cap} {e.pool} job(s) "
            f"running in this session. Wait for one to finish before "
            f"queuing another."
        )
    else:
        st.error(
            f"The {e.pool} pool is busy ({e.current}/{e.cap} jobs in "
            f"flight across all sessions). Try again in a few minutes."
        )


# Phase C C5: queue → pool routing for the per-pool depth readout.
# Mirrors orchestrator/__main__.py:POOL_QUEUES + celery_app.task_routes.
_QUEUE_TO_POOL = {"default": "fast", "celery": "fast", "docking": "docking"}


def queue_depth_per_pool() -> Optional[dict[str, int]]:
    """Best-effort per-pool count of jobs not yet running.

    Returns ``{"fast": N, "docking": M}`` on success, ``None`` on
    catastrophic failure. Individual missing data points (Redis
    unreachable, Celery inspect timing out) degrade silently — a 0 just
    means "we couldn't tell," same shape as the pre-C5 single-int form.

    Sums each queue's broker llen + reserved-on-worker counts, then
    rolls those up by pool via :data:`_QUEUE_TO_POOL`. Renamed from the
    B11.21 ``queue_depth_ahead`` so panels know which slot type a task
    is actually waiting for.
    """
    try:
        from config import Config

        totals = {"fast": 0, "docking": 0}
        # Broker queue length per queue name.
        try:
            import redis

            r = redis.from_url(Config.CELERY_BROKER_URL)
            for queue, pool in _QUEUE_TO_POOL.items():
                totals[pool] += int(r.llen(queue) or 0)
        except Exception:
            pass
        # Reserved per-worker — split by the worker's queue (best-effort).
        try:
            from celery_app import celery_app

            inspect = celery_app.control.inspect(timeout=1.0)
            reserved = inspect.reserved() or {}
            active_queues = inspect.active_queues() or {}
            for worker, tasks in reserved.items():
                # Look up the worker's bound queues; fall back to fast.
                qs = active_queues.get(worker, [])
                names = {q.get("name", "default") for q in qs if isinstance(q, dict)}
                pool = "docking" if "docking" in names else "fast"
                totals[pool] += len(tasks or [])
        except Exception:
            pass
        return totals
    except Exception:
        return None


def queue_depth_ahead() -> Optional[int]:
    """Back-compat wrapper around :func:`queue_depth_per_pool`.

    Sums every pool's depth into one number for older call sites that
    haven't switched to the per-pool form yet. New code should use the
    pool-aware variant so the PENDING caption can say *which* pool is
    busy.
    """
    per_pool = queue_depth_per_pool()
    if per_pool is None:
        return None
    return sum(per_pool.values())


def render_running_progress(task, default_label: str, *, pool: Optional[str] = None) -> None:
    """Render the live progress payload for an in-flight task.

    Pure renderer — no side effects. The polling cadence is owned by
    the caller's enclosing ``@st.fragment(run_every="3s")``; matches
    the docking panel's ``_docking_running_fragment``. The previous
    ``time.sleep(3); st.rerun()`` tail caused the whole page to be
    appended to the DOM on every tick (the new render started while
    the old one was still visible) — fixed by moving the polling out
    of this helper and into a fragment.

    ``pool`` (C5) names the worker pool the task was routed to —
    ``"fast"`` for find_pockets / cluster, ``"docking"`` for docking.
    When supplied, the PENDING caption tells the user which pool is
    busy so they know the wait time is bounded by that pool's
    occupancy.
    """
    info = task.info if isinstance(task.info, dict) else {}
    progress = int(info.get("progress") or 0)
    step = info.get("current_step") or default_label

    if task.state == "PENDING":
        st.info("Queued — waiting for a worker to pick this up.")
        st.progress(0)
        # C5: per-pool depth gives "waiting for fast" vs "waiting for docking".
        per_pool = queue_depth_per_pool()
        if per_pool is not None and pool in per_pool:
            ahead = max(0, per_pool[pool] - 1)
            if ahead > 0:
                st.caption(
                    f"~{ahead} job(s) ahead of this one in the **{pool}** pool."
                )
        elif per_pool is not None:
            # No pool hint from the caller — fall back to total.
            total = sum(per_pool.values())
            if total > 1:
                st.caption(f"~{total - 1} job(s) ahead of this one in the queue.")
    else:
        st.progress(progress / 100, text=f"{progress}% · {step}")

    # Optional contextual metrics — surfaced when the task reports them.
    # B11.21: find_pockets / cluster show elapsed time only — their
    # progress bar is a synthetic log-ramp, so an ETA would mislead.
    extras = []
    elapsed = info.get("elapsed")
    if elapsed is not None:
        try:
            extras.append(f"{int(elapsed)}s elapsed")
        except (TypeError, ValueError):
            pass
    for key, label in (
        ("frames_extracted", "frames"),
        ("pockets_detected", "pockets"),
        ("pairs_done", None),  # rendered specially below
    ):
        val = info.get(key)
        if val is None:
            continue
        if key == "pairs_done":
            total = info.get("pairs_total")
            if total:
                extras.append(f"pairs {val}/{total}")
            else:
                extras.append(f"pairs {val}")
        else:
            extras.append(f"{val} {label}")
    if extras:
        st.caption(" · ".join(extras))


def render_failure(task, job_id: str, *, panel_prefix: str) -> None:
    """Render the structured failure panel + a "Re-run with new settings" button.

    ``panel_prefix`` is one of ``"find_pockets"`` / ``"cluster"`` /
    ``"docking"`` — used to know which ``st.session_state.<prefix>_*``
    keys to clear when the user clicks "Re-run".
    """
    from failure_view import load_status_json, render_task_failure

    info = task.info if isinstance(task.info, dict) else {}
    render_task_failure(info, load_status_json(job_id), job_id)

    if st.button(
        "Re-run with new settings",
        key=f"{panel_prefix}_clear_after_failure",
    ):
        for suffix in ("task_id", "job_id", "status"):
            st.session_state.pop(f"{panel_prefix}_{suffix}", None)
        st.rerun()
