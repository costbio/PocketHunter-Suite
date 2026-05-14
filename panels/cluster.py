"""Cluster stage panel (v2 analysis app).

Three-state shape mirrors the Pockets panel: settings → running →
results. Source pockets come from a session-scoped dropdown (no
free-text job_id paste in v2); cluster params follow the legacy form
(min_prob, method, dbscan_hierarchical).

Results lean on the existing :func:`cluster_visualization.render_consensus_panel`
to render the 3-column heatmap + checkbox + mini-viewer block — the
panel only owns layout chrome around it.
"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from config import Config
from rate_limiter import RateLimitExceeded, check_task_rate_limit
from security import FileValidator, SecurityError
from session_routes import register_session_job
from tasks import run_cluster_pockets_task

from panels._shared import (
    get_async_result,
    job_by_legacy_id,
    latest_job_of_kind,
    new_job_id,
    pipeline_in_flight,
    render_failure,
    render_running_progress,
)


_PANEL = "cluster"
_RESULTS_KINDS = ("cluster", "pipeline")
_SOURCE_KINDS = ("find_pockets", "pipeline")


@st.cache_data(show_spinner=False)
def _load_cluster_csvs(reps_csv_path: str, clustered_csv_path: str):
    """Cached cluster-output CSV loader.

    Cluster job outputs are written once and immutable for the life of
    the job, so caching by full path string is safe — re-reads only
    happen when the path itself changes (different cluster job ID).
    Cache lives in-process; cleared by ``cleanup_job`` pruning the
    per-session results dir, or by a worker restart.

    Returns ``(df_reps, df_clustered_or_None)``. ``df_clustered`` is
    pre-filtered to drop ``cluster == -1`` noise rows.
    """
    df_reps = pd.read_csv(reps_csv_path)
    df_clustered = None
    if os.path.exists(clustered_csv_path):
        try:
            df_clustered = pd.read_csv(clustered_csv_path)
            if "cluster" in df_clustered.columns:
                df_clustered = df_clustered[df_clustered["cluster"] != -1]
        except Exception:
            df_clustered = None
    return df_reps, df_clustered


def _live_task_or_none():
    cluster_id = st.session_state.get("cluster_task_id")
    if cluster_id:
        task = get_async_result(cluster_id)
        if task is not None:
            return task
    return None


def _list_source_options(session_id) -> list[dict]:
    """All session jobs that produced a pockets.csv, newest-first."""
    from failure_view import load_status_for_session

    return [
        row
        for row in load_status_for_session(session_id)
        if row.get("kind") in _SOURCE_KINDS
        and row.get("status") in ("completed", "SUCCESS", "success")
    ]


def _pockets_csv_for(legacy_id: str) -> str:
    return os.path.join(str(Config.RESULTS_DIR), legacy_id, "pockets", "pockets.csv")


def _render_settings(session, is_editor: bool) -> None:
    pipeline_busy = pipeline_in_flight()
    if pipeline_busy:
        st.warning(
            "Pipeline run in progress — its cluster stage will produce a "
            "result shortly. Wait or cancel before kicking off a separate "
            "Cluster job."
        )

    sources = _list_source_options(session.id)
    if not sources:
        st.info(
            "Run **Find Pockets** first — the cluster step needs a "
            "pockets.csv to operate on."
        )
        return

    labels = []
    legacy_ids = []
    for s in sources:
        legacy_id = s.get("legacy_id") or "(unnamed)"
        kind = s.get("kind", "?")
        ts = s.get("last_updated") or ""
        labels.append(f"{kind} · {legacy_id} · {ts[:19]}")
        legacy_ids.append(legacy_id)

    idx = st.selectbox(
        "Source pockets",
        options=list(range(len(sources))),
        format_func=lambda i: labels[i],
        key="cluster_source_idx",
        help="Pick which find_pockets job to cluster.",
    )
    chosen_legacy_id = legacy_ids[idx] if idx is not None else None

    min_prob = st.slider(
        "Min. ligand-binding probability",
        min_value=0.0,
        max_value=1.0,
        value=0.5,
        step=0.05,
        key="cluster_min_prob",
        help="Pockets below this p2rank probability are filtered before clustering.",
    )

    method = st.selectbox(
        "Method",
        options=["dbscan", "hierarchical"],
        index=0,
        key="cluster_method",
        help="DBSCAN groups dense regions; Hierarchical always produces clusters.",
    )

    if method == "dbscan":
        st.checkbox(
            "Hierarchical refinement",
            value=True,
            key="cluster_dbscan_hierarchical",
            help="Apply sub-clustering within each DBSCAN cluster.",
        )

    disabled = (not is_editor) or pipeline_busy or chosen_legacy_id is None
    if not st.button(
        "Cluster",
        type="primary",
        use_container_width=True,
        disabled=disabled,
        key="cluster_submit",
    ):
        return

    _submit(chosen_legacy_id, min_prob, method)


def _submit(source_legacy_id: str, min_prob: float, method: str) -> None:
    try:
        source_legacy_id = FileValidator.validate_job_id(source_legacy_id)
    except SecurityError as e:
        st.error(f"Invalid source job ID: {e}")
        return

    csv_path = _pockets_csv_for(source_legacy_id)
    if not os.path.exists(csv_path):
        st.error(
            f"pockets.csv missing for the chosen source — the source job's "
            "results may have been pruned. Run Find Pockets again."
        )
        return

    try:
        check_task_rate_limit()
    except RateLimitExceeded as e:
        st.error(f"Task rate limit exceeded — wait {e.retry_after:.0f}s.")
        return

    job_id = new_job_id("cluster")
    register_session_job(job_id, "cluster")

    dbscan_hier = bool(st.session_state.get("cluster_dbscan_hierarchical", True)) if method == "dbscan" else False

    task = run_cluster_pockets_task.delay(
        pockets_csv_path_abs=os.path.abspath(csv_path),
        job_id=job_id,
        min_prob=min_prob,
        clustering_method=method,
        dbscan_hierarchical=dbscan_hier,
    )
    st.session_state.cluster_job_id = job_id
    st.session_state.cluster_task_id = task.id
    st.session_state.cluster_status = "running"
    # B11.10: clear the force-settings flag — fresh task is the
    # transition we held the settings view to enable.
    st.session_state.pop("cluster_force_settings", None)
    st.rerun()


def _render_running(task) -> None:
    state = task.state
    if state == "FAILURE":
        render_failure(
            task,
            st.session_state.get("cluster_job_id", ""),
            panel_prefix=_PANEL,
        )
        return
    if state == "SUCCESS":
        st.session_state.pop("cluster_task_id", None)
        st.session_state.cluster_status = "completed"
        st.rerun()
        return
    render_running_progress(task, "Clustering pockets…")


def _render_clustered_pockets_tab(
    df_clustered: pd.DataFrame,
    pdb_source_job_id: str,
    is_editor: bool,
) -> None:
    """Filter + multi-select pocket browser.

    B11.5: filter widgets (probability slider + cluster multiselect)
    live inside an ``st.form``. Form widgets don't trigger reruns
    until the user clicks "Apply" — so dragging the slider is
    instantaneous, the Mol* viewer stays put, and the table only
    re-renders once on Apply. Last-applied values persist in
    session_state so the table remembers the filter across reruns.

    (B11.4 tried ``@st.fragment`` for the same goal but fragments
    nested inside ``st.tabs`` don't reliably isolate widget events
    in Streamlit 1.52.2 — slider drag still triggered full page
    reruns. ``st.form`` is the deterministic alternative.)
    """
    from panels import _docking_bucket as docking_bucket

    df_pocket_view = df_clustered.copy()
    if "num_residues" not in df_pocket_view.columns and "residues" in df_pocket_view.columns:
        df_pocket_view["num_residues"] = df_pocket_view["residues"].apply(
            lambda x: len(str(x).split()) if pd.notna(x) else 0
        )

    available_clusters = sorted(
        int(c) for c in df_pocket_view["cluster"].dropna().unique()
    )

    # Last-applied filter values. The form widgets read these as their
    # default values; the table reads them to filter. Form widget keys
    # are distinct (``_form`` suffix) so widgets and persisted values
    # don't collide.
    eff_min_p = float(st.session_state.get("cluster_pocket_minp", 0.0))
    persisted_clusters = st.session_state.get(
        "cluster_pocket_cluster_filter", available_clusters,
    )
    # Drop persisted cluster IDs that no longer exist (different cluster job).
    eff_clusters = [c for c in persisted_clusters if c in available_clusters] or available_clusters

    with st.form("cluster_pocket_filter", border=False):
        fc1, fc2, fc3 = st.columns([2, 3, 1])
        with fc1:
            min_p_form = st.slider(
                "Min probability", 0.0, 1.0,
                value=eff_min_p, step=0.05,
                key="cluster_pocket_minp_form",
            )
        with fc2:
            picked_form = st.multiselect(
                "Clusters",
                options=available_clusters,
                default=eff_clusters,
                key="cluster_pocket_cluster_filter_form",
                help="Restrict the table to specific cluster IDs.",
            )
        with fc3:
            # Spacer so the Apply button visually aligns with the
            # widget rows above (they have label headers).
            st.markdown(
                "<div style='height:28px'></div>",
                unsafe_allow_html=True,
            )
            applied = st.form_submit_button(
                "Apply", use_container_width=True,
            )

    if applied:
        st.session_state["cluster_pocket_minp"] = float(min_p_form)
        st.session_state["cluster_pocket_cluster_filter"] = list(picked_form)
        eff_min_p = float(min_p_form)
        eff_clusters = list(picked_form) or available_clusters

    df_filtered = df_pocket_view[
        (df_pocket_view["probability"] >= eff_min_p)
        & (df_pocket_view["cluster"].isin(eff_clusters))
    ].sort_values(
        ["cluster", "probability"], ascending=[True, False],
        kind="mergesort",
    ).reset_index(drop=True)

    disp_cols = [c for c in (
        "cluster", "Frame", "pocket_index", "probability", "num_residues"
    ) if c in df_filtered.columns]
    # B11.9: restore per-row multi-select. B11.6 had stripped it
    # because row clicks triggered slow page reruns; B11.8's lazy
    # tab dispatch + dropped idle fragment polling cut the rerun
    # cost enough that this is responsive again.
    pocket_sel = st.dataframe(
        df_filtered[disp_cols],
        use_container_width=True,
        height=300,
        on_select="rerun",
        selection_mode="multi-row",
        key="cluster_pocket_table",
    )
    sel_rows = (
        (pocket_sel.selection.get("rows") or [])
        if pocket_sel and getattr(pocket_sel, "selection", None)
        else []
    )
    st.caption(
        f"Showing {len(df_filtered)} of {len(df_pocket_view)} pockets. "
        "Click rows; ctrl/shift-click to multi-select; then Add."
    )

    ac1, ac2 = st.columns(2)
    with ac1:
        if st.button(
            f"Add {len(sel_rows)} selected → docking"
            if sel_rows else "Add selected → docking",
            key="cluster_add_selected_pockets",
            use_container_width=True,
            type="primary" if sel_rows else "secondary",
            disabled=(not is_editor) or not pdb_source_job_id or not sel_rows,
        ):
            rows_to_add = df_filtered.iloc[sel_rows]
            entries = docking_bucket.from_pockets_df(
                rows_to_add, source_job_id=pdb_source_job_id,
            )
            n_added = docking_bucket.add(entries)
            if n_added:
                st.success(f"Added {n_added} pocket(s) to docking.")
            else:
                st.info("Those pockets are already in the docking selection.")
            st.rerun()
    with ac2:
        if st.button(
            f"Add all {len(df_filtered)} filtered → docking",
            key="cluster_add_all_filtered",
            use_container_width=True,
            disabled=(
                (not is_editor)
                or not pdb_source_job_id
                or len(df_filtered) == 0
            ),
            help="Adds every row currently shown above to the docking selection.",
        ):
            entries = docking_bucket.from_pockets_df(
                df_filtered, source_job_id=pdb_source_job_id,
            )
            n_added = docking_bucket.add(entries)
            if n_added:
                st.success(f"Added {n_added} pocket(s) to docking.")
            else:
                st.info("Those pockets are already in the docking selection.")
            st.rerun()

    if available_clusters:
        st.markdown("**Add all members of a cluster:**")
        btn_cols = st.columns(len(available_clusters))
        for col, cid in zip(btn_cols, available_clusters):
            clust_df = df_pocket_view[df_pocket_view["cluster"] == cid]
            n_pockets = len(clust_df)
            with col:
                if st.button(
                    f"C{cid} ({n_pockets})",
                    key=f"cluster_add_all_c{cid}",
                    use_container_width=True,
                    disabled=(not is_editor) or not pdb_source_job_id,
                    help=f"Add all {n_pockets} pocket(s) in cluster {cid}.",
                ):
                    entries = docking_bucket.from_pockets_df(
                        clust_df, source_job_id=pdb_source_job_id,
                        cluster_override=int(cid),
                    )
                    n_added = docking_bucket.add(entries)
                    if n_added:
                        st.success(f"Added {n_added} pocket(s) from C{cid} to docking.")
                    else:
                        st.info(f"All C{cid} pockets are already in the docking selection.")
                    st.rerun()


def _render_results(latest_job: dict, is_editor: bool, session) -> None:
    results_job_id = latest_job.get("legacy_id") or ""
    try:
        results_job_id = FileValidator.validate_job_id(results_job_id)
    except SecurityError as e:
        st.error(f"Invalid job ID for results: {e}")
        return

    results_dir = str(Config.RESULTS_DIR)
    cluster_dir = os.path.join(results_dir, results_job_id, "pocket_clusters")
    reps_csv = os.path.join(cluster_dir, "cluster_representatives.csv")

    header_l, header_r = st.columns([3, 1])
    with header_l:
        st.caption(f"Latest run: `{results_job_id}`")
    with header_r:
        if st.button(
            "Re-cluster",
            key="cluster_rerun_from_results",
            disabled=not is_editor,
            use_container_width=True,
            help="Clear and submit a new cluster job.",
        ):
            # B11.10: see panels/find_pockets.py Re-run handler.
            for k in ("cluster_task_id", "cluster_job_id", "cluster_status"):
                st.session_state.pop(k, None)
            st.session_state["cluster_force_settings"] = True
            st.rerun()

    if not os.path.exists(reps_csv):
        st.warning(
            "Latest cluster job completed but cluster_representatives.csv is "
            "missing on disk — session results may have been pruned."
        )
        return

    clustered_csv = os.path.join(cluster_dir, "pockets_clustered.csv")
    try:
        # B11.4: cached read — cluster outputs are immutable so caching
        # by path string is safe and avoids re-reading the (potentially
        # large) clustered CSV on every rerun.
        df_reps, df_clustered = _load_cluster_csvs(reps_csv, clustered_csv)
    except Exception as e:
        st.error(f"Could not read cluster_representatives.csv: {e}")
        return

    if "residues" in df_reps.columns and df_reps["residues"].dtype == object:
        df_reps["num_residues"] = df_reps["residues"].apply(
            lambda x: len(str(x).split()) if pd.notna(x) else 0
        )
    elif "residues" in df_reps.columns:
        df_reps["num_residues"] = df_reps["residues"]
    else:
        df_reps["num_residues"] = 0

    if len(df_reps) == 0:
        st.warning("Clustering completed but no representative pockets were found.")
        st.info(
            "Try lowering min_prob, reducing the trajectory stride to extract "
            "more frames, or switching to Hierarchical."
        )
        return

    # B11: compact one-line stats strip in place of the 2×2 ``st.metric``
    # grid. Same shape as the find_pockets panel for consistency.
    st.markdown(
        f"**{len(df_reps)} clusters** &nbsp;·&nbsp; "
        f"avg p={df_reps['probability'].mean():.2f} &nbsp;·&nbsp; "
        f"avg residues={df_reps['num_residues'].mean():.0f} &nbsp;·&nbsp; "
        f"best p={df_reps['probability'].max():.2f}"
    )

    # Cluster annotations are derived by the viewer fragment in
    # ``analysis_app.py`` via ``derive_session_annotations``; the panel
    # no longer pushes them directly.

    # B11.2: resolve the *source* find_pockets / pipeline job whose
    # ``pdbs/`` dir owns the per-frame PDBs. The cluster job has no
    # pdbs dir, so the heatmap's frame-jump lookup was failing
    # silently — see B11.2 plan, issue 2.
    from panels._shared import latest_job_of_kind

    source_job = latest_job_of_kind(session.id, ("find_pockets", "pipeline"))
    pdb_source_job_id = ""
    if source_job is not None:
        candidate = source_job.get("legacy_id") or ""
        try:
            pdb_source_job_id = FileValidator.validate_job_id(candidate)
        except SecurityError:
            pdb_source_job_id = ""

    # B11.8: replace ``st.tabs`` (which renders ALL four tab bodies
    # on every page rerun) with a ``st.segmented_control`` driving
    # an if/elif dispatch. Only the active tab's body executes — the
    # heatmap subplot construction + ``plotly_events`` mount no
    # longer runs when the user is on Clustered pockets / Reps /
    # Downloads. That was the single biggest hidden cost per rerun.
    _CLUSTER_TAB_OPTIONS = [
        "Heatmap", "Clustered pockets", "Representatives", "Downloads",
    ]
    _active_tab_key = f"cluster_tab_{session.short_code}"
    st.session_state.setdefault(_active_tab_key, "Clustered pockets")
    active_tab = st.segmented_control(
        "Cluster view",
        options=_CLUSTER_TAB_OPTIONS,
        default=st.session_state[_active_tab_key],
        key=_active_tab_key,
        label_visibility="collapsed",
    )

    safe_short = session.short_code.replace("__", "_")

    if active_tab == "Heatmap":
        if df_clustered is not None and len(df_clustered) > 0:
            from cluster_visualization import render_per_pocket_heatmap

            render_per_pocket_heatmap(
                df_clustered, df_reps, results_job_id,
                key_prefix="panel_cluster",
                session_short=safe_short,
                pdb_source_job_id=pdb_source_job_id,
            )
        else:
            st.info(
                "pockets_clustered.csv not available for this job — the "
                "heatmap requires the per-pocket residue matrix."
            )

    elif active_tab == "Clustered pockets":
        if df_clustered is None or len(df_clustered) == 0:
            st.info("pockets_clustered.csv not available for this job.")
        else:
            _render_clustered_pockets_tab(
                df_clustered, pdb_source_job_id, is_editor,
            )

    elif active_tab == "Representatives":
        from cluster_labels import describe_cluster_spatially
        from panels import _docking_bucket as docking_bucket

        if st.button(
            f"Add all {len(df_reps)} cluster representatives → docking",
            use_container_width=True,
            type="primary",
            disabled=(not is_editor) or not pdb_source_job_id,
            help=(
                "One pocket per cluster (the medoid). Classical "
                "ensemble-docking starting point."
            ),
            key="cluster_add_reps",
        ):
            entries = []
            if "cluster" in df_reps.columns:
                for _, rep in df_reps.iterrows():
                    sub = df_reps[df_reps["cluster"] == rep["cluster"]]
                    entries.extend(docking_bucket.from_pockets_df(
                        sub, source_job_id=pdb_source_job_id,
                        cluster_override=int(rep["cluster"]),
                    ))
            else:
                entries = docking_bucket.from_pockets_df(
                    df_reps, source_job_id=pdb_source_job_id,
                )
            n_added = docking_bucket.add(entries)
            if n_added:
                st.success(f"Added {n_added} representative(s) to docking.")
            else:
                st.info("Cluster representatives already in the docking selection.")

        df_display = df_reps.sort_values("probability", ascending=False).copy()

        def _quality(p: float) -> str:
            if p >= 0.8:
                return "Excellent"
            if p >= 0.6:
                return "Good"
            if p >= 0.4:
                return "Moderate"
            return "Low"

        df_display["Quality"] = df_display["probability"].apply(_quality)
        if "residues" in df_display.columns:
            df_display["Location"] = df_display["residues"].apply(describe_cluster_spatially)
        else:
            df_display["Location"] = "—"

        cols = ["Cluster", "Frame", "Location", "probability", "num_residues", "Quality"]
        if "cluster" in df_display.columns:
            df_display["Cluster"] = df_display["cluster"].astype("Int64")
        else:
            cols.remove("Cluster")
        cols = [c for c in cols if c in df_display.columns]

        # B11.9: selectable reps table + "Add N selected reps" button.
        reps_sel = st.dataframe(
            df_display[cols],
            use_container_width=True,
            height=320,
            on_select="rerun",
            selection_mode="multi-row",
            key="cluster_reps_table",
        )
        reps_rows = (
            (reps_sel.selection.get("rows") or [])
            if reps_sel and getattr(reps_sel, "selection", None)
            else []
        )
        if reps_rows:
            if st.button(
                f"Add {len(reps_rows)} selected rep(s) → docking",
                key="cluster_add_selected_reps",
                use_container_width=True,
                type="primary",
                disabled=(not is_editor) or not pdb_source_job_id,
            ):
                rows_to_add = df_display.iloc[reps_rows]
                entries: list[dict] = []
                for _, row in rows_to_add.iterrows():
                    cid = None
                    if "cluster" in rows_to_add.columns and pd.notna(row.get("cluster")):
                        try:
                            cid = int(row["cluster"])
                        except (TypeError, ValueError):
                            cid = None
                    sub = pd.DataFrame([row])
                    entries.extend(docking_bucket.from_pockets_df(
                        sub, source_job_id=pdb_source_job_id,
                        cluster_override=cid,
                    ))
                n_added = docking_bucket.add(entries)
                if n_added:
                    st.success(f"Added {n_added} rep(s) to docking.")
                else:
                    st.info("Those reps are already in the docking selection.")
                st.rerun()

    elif active_tab == "Downloads":
        st.download_button(
            "cluster_representatives.csv",
            data=df_reps.to_csv(index=False),
            file_name=f"cluster_representatives_{results_job_id}.csv",
            mime="text/csv",
            use_container_width=True,
        )
        df_hq = df_reps[df_reps["probability"] >= 0.7]
        if len(df_hq) > 0:
            st.download_button(
                "high-quality subset",
                data=df_hq.to_csv(index=False),
                file_name=f"high_quality_clusters_{results_job_id}.csv",
                mime="text/csv",
                use_container_width=True,
            )

    # B11.2: gate the Dock CTA on the docking bucket (was: cluster
    # checkbox selection). The bucket is populated from this panel's
    # "Add cluster representatives" preset, the per-pocket table, or
    # the Pockets stage table.
    st.divider()
    from panels import _docking_bucket as docking_bucket

    bucket_count = docking_bucket.count()
    has_selection = bucket_count > 0
    if st.button(
        f"Next: Dock →  ({bucket_count} pocket{'s' if bucket_count != 1 else ''} selected)",
        type="primary",
        use_container_width=True,
        disabled=(not is_editor) or (not has_selection),
        key="cluster_next_dock",
        help=(
            "Switch to the Docking panel."
            if has_selection
            else "Add at least one pocket to the docking selection (preset above or table)."
        ),
    ):
        # B11.1: see panels/find_pockets.py "Next: Cluster →" comment.
        st.session_state["pending_active_stage"] = "Dock"
        st.rerun()


def render(session, is_editor: bool) -> None:
    task = _live_task_or_none()
    if task is not None:
        _render_running(task)
        return

    # B11.10: Re-cluster button sets this flag; honor it before the
    # DB-driven results dispatch.
    if st.session_state.get("cluster_force_settings"):
        _render_settings(session, is_editor)
        return

    # B11.20: a Jobs-list click can pin a specific (possibly non-latest)
    # cluster job via cluster_view_job_id; fall back to the latest run
    # when unset or stale.
    view_id = st.session_state.get("cluster_view_job_id")
    latest = job_by_legacy_id(session.id, view_id) if view_id else None
    if latest is None:
        latest = latest_job_of_kind(session.id, _RESULTS_KINDS)
    if latest is not None:
        _render_results(latest, is_editor, session)
        return

    _render_settings(session, is_editor)
