"""Helpers shared by the Pockets / Cluster / Dock panels.

Three states each panel walks through — settings → running → results —
share a small set of primitives: looking up the latest completed Job of a
given ``kind`` from the DB, detecting whether a pipeline run-all task is
currently in flight, rendering the polling progress bar + sleep+rerun
loop, and rendering structured failures. Centralised here so the three
panel modules stay focused on their own forms + results UI.
"""
from __future__ import annotations

import time
import uuid
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


def frame_index_for_filename(results_job_id: str, file_name: str) -> Optional[int]:
    """Resolve the 1-based model index for a frame's source PDB filename.

    pockethunter's ``pockets.csv`` carries the file name with a
    ``_predictions`` suffix appended by p2rank (e.g.
    ``..._fit_84.pdb_predictions``). Strip that and ensure the
    trailing ``.pdb`` is present before looking up the model index.

    Returns ``None`` when the file isn't present in the job's pdbs dir
    (e.g. pruned, renamed, or a typo in pockets.csv). Callers fall
    back to the current slider value.
    """
    if not results_job_id or not file_name:
        return None
    name = str(file_name).strip()
    if name.endswith("_predictions"):
        name = name[: -len("_predictions")]
    if not name.endswith(".pdb"):
        name = f"{name}.pdb"
    return _frame_index_map(results_job_id).get(name)


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
    """``True`` when a "Run all stages" pipeline task is still running.

    Used to disable per-stage submit buttons while a chained run owns
    the session. Reads :class:`celery.result.AsyncResult` for the
    ``st.session_state.pipeline_task_id`` set by the run-all handler.
    """
    task_id = st.session_state.get("pipeline_task_id")
    if not task_id:
        return False
    try:
        from celery_app import celery_app

        task = celery_app.AsyncResult(task_id)
        return task.state in _RUNNING_STATES
    except Exception:
        return False


def any_panel_task_running() -> bool:
    """``True`` when any tracked panel task (pipeline or single-stage) is live.

    Used by the Run-all button to refuse a second submission while the
    session already has something on the queue. Cheap — at most four
    AsyncResult lookups against Redis.
    """
    keys = (
        "pipeline_task_id",
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


def queue_depth_ahead() -> Optional[int]:
    """Best-effort count of jobs sitting in the Celery queues (B11.21).

    Sums the broker queue lengths (``default`` + ``docking``) plus tasks
    a worker has reserved (prefetched) but not yet started. With
    ``worker_prefetch_multiplier = 1`` this tracks "jobs not yet
    running". Returns ``None`` on any failure so callers degrade
    silently — it is a courtesy hint, not a hard guarantee.
    """
    try:
        from config import Config

        total = 0
        try:
            import redis

            r = redis.from_url(Config.CELERY_BROKER_URL)
            for queue in ("default", "docking"):
                total += int(r.llen(queue) or 0)
        except Exception:
            pass
        try:
            from celery_app import celery_app

            reserved = celery_app.control.inspect(timeout=1.0).reserved() or {}
            total += sum(len(v) for v in reserved.values())
        except Exception:
            pass
        return total
    except Exception:
        return None


def render_running_progress(task, default_label: str) -> None:
    """Render the live progress payload for an in-flight task and rerun.

    Pulls ``progress`` + ``current_step`` from ``task.info`` when present,
    falls back to a placeholder when the task is still PENDING, sleeps
    3 s, and reruns. The viewer + jobs panel separately poll via
    ``@st.fragment(run_every="3s")`` (see :mod:`analysis_app`) so the
    persistent viewer + job list update without a full rerun; the
    panel-level sleep+rerun here only drives the in-panel progress bar.
    """
    info = task.info if isinstance(task.info, dict) else {}
    progress = int(info.get("progress") or 0)
    step = info.get("current_step") or default_label

    if task.state == "PENDING":
        st.info("Queued — waiting for a worker to pick this up.")
        st.progress(0)
        # B11.21: surface how many jobs are waiting ahead of this one.
        ahead = queue_depth_ahead()
        if ahead is not None and ahead > 1:
            st.caption(f"~{ahead - 1} job(s) ahead of this one in the queue.")
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

    time.sleep(3)
    st.rerun()


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
