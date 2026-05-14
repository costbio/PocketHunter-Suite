"""Session-scoped jobs panel for the v2 analysis app.

Renders the active session's jobs newest-first under a Streamlit
fragment that polls every 3 s. The v1 global Task Monitor page is
gone; this panel is the only job-status surface.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import streamlit as st


# Visual indicator per status. The keys cover both the lowercase
# disk-style writes and the uppercase Celery-state writes ``tasks.py``
# sometimes emits.
_STATUS_INDICATOR = {
    "completed": "●",
    "SUCCESS": "●",
    "success": "●",
    "running": "◐",
    "PROGRESS": "◐",
    "submitted": "○",
    "PENDING": "○",
    "failed": "✕",
    "FAILURE": "✕",
}

# B11.20: which job kinds are clickable, and where a click navigates —
# ``kind -> (active_stage, "<stage>_view_job_id" session-state key)``.
_CLICKABLE_KINDS = {
    "find_pockets": ("Pockets", "fp_view_job_id"),
    "cluster": ("Cluster", "cluster_view_job_id"),
    "docking": ("Dock", "docking_view_job_id"),
}
_DONE_STATUSES = {"completed", "SUCCESS", "success"}


def _format_duration(start_iso: Optional[str], end_iso: Optional[str]) -> str:
    """Compose ``HH:MM:SS`` or ``Nd Nh`` between two ISO timestamps.

    Returns ``"—"`` when either timestamp is missing or unparseable.
    """
    if not start_iso or not end_iso:
        return "—"
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
    except (TypeError, ValueError):
        return "—"
    delta: timedelta = end - start
    if delta.total_seconds() < 0:
        return "—"
    total_s = int(delta.total_seconds())
    if delta.days > 0:
        return f"{delta.days}d {total_s % 86400 // 3600}h"
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def _row_summary(row: dict) -> str:
    kind = row.get("kind", "?")
    status = row.get("status", "?")
    indicator = _STATUS_INDICATOR.get(status, "·")
    legacy = row.get("legacy_id") or "—"
    return f"{indicator} **{kind}** · `{legacy}` · *{status}*"


def render(session) -> None:
    """Render the jobs panel for ``session``.

    Called from inside a ``@st.fragment(run_every="3s")`` in
    ``analysis_app.py``. The fragment owns the polling cadence.
    """
    from failure_view import (
        load_status_for_session,
        render_ligand_conversion_callout,
        render_pair_failures_callout,
        render_task_failure,
    )

    if session is None:
        st.caption("No active session.")
        return

    jobs = load_status_for_session(session.id) or []
    if not jobs:
        st.caption("No jobs yet. Submit a stage from the panel above to start one.")
        return

    # ``failure_view.load_status_for_session`` doesn't currently expose
    # ``created_at``; ``last_updated`` is the only timestamp on each
    # row. Render that as the row's anchor time + use it for ordering.
    sorted_jobs = sorted(
        jobs,
        key=lambda r: r.get("last_updated") or "",
        reverse=True,
    )

    for row in sorted_jobs:
        with st.container(border=True):
            status = row.get("status")
            kind = row.get("kind")
            legacy = row.get("legacy_id") or ""
            # B11.20: completed find_pockets / cluster / docking jobs are
            # clickable — open that exact run in its stage panel via the
            # ``<stage>_view_job_id`` override.
            nav = _CLICKABLE_KINDS.get(kind)
            if nav and status in _DONE_STATUSES and legacy:
                stage, view_key = nav
                if st.button(
                    _row_summary(row),
                    key=f"jobrow_{legacy}",
                    use_container_width=True,
                ):
                    st.session_state["pending_active_stage"] = stage
                    st.session_state[view_key] = legacy
                    st.rerun(scope="app")
            else:
                st.markdown(_row_summary(row))

            ts = row.get("last_updated") or "—"
            st.caption(f"Updated: {ts[:19]}")

            if status in ("failed", "FAILURE"):
                with st.expander("Failure details", expanded=False):
                    render_task_failure(
                        row.get("error") or {},
                        # render_task_failure happily accepts the row dict
                        # itself as ``status_json``; it just looks up
                        # nested ``.error.log_path``.
                        {"error": row.get("error") or {}},
                        row.get("legacy_id"),
                    )
            else:
                # Partial-success callouts for docking rows that completed
                # but had some pair failures or ligand-conversion shortfalls.
                result_info = row.get("result_info") or {}
                if isinstance(result_info, dict):
                    if result_info.get("pairs_failed"):
                        render_pair_failures_callout(result_info, row.get("legacy_id"))
                    render_ligand_conversion_callout(result_info, row.get("legacy_id"))
