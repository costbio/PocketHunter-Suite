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
import re

import pandas as pd
import streamlit as st

from config import Config
from rate_limiter import RateLimitExceeded, check_task_rate_limit
from security import FileValidator, SecurityError
from session_routes import register_session_job
from tasks import run_cluster_pockets_task

from panels._shared import (
    PoolCapHit,
    assert_submit_allowed,
    get_async_result,
    job_by_legacy_id,
    latest_job_of_kind,
    new_job_id,
    render_failure,
    render_pool_cap_error,
    render_running_progress,
)


_PANEL = "cluster"
_RESULTS_KINDS = ("cluster",)
_SOURCE_KINDS = ("find_pockets",)


_RESIDUE_COL_RE = re.compile(r"^[A-Z]_\d+$")
_HIERARCHICAL_FILE_RE = re.compile(r"cluster_(\d+)_hierarchical\.csv$")


def _medoid_index(residue_matrix) -> int:
    """Row index whose mean Hamming distance to all others is smallest.

    Pure NumPy — same primitive PocketHunter uses for DBSCAN medoids.
    Single-row input returns 0; empty input returns -1.
    """
    import numpy as np

    n = residue_matrix.shape[0]
    if n == 0:
        return -1
    if n == 1:
        return 0
    M = residue_matrix.astype(bool)
    # n x n mean Hamming distance matrix. Cheap for the cluster sizes
    # PocketHunter produces (tens to low thousands of rows per file).
    dists = np.empty((n, n), dtype=float)
    for i in range(n):
        dists[i] = (M[i] != M).mean(axis=1)
    return int(dists.mean(axis=1).argmin())


def _hierarchical_parents(cluster_dir: str) -> list[int]:
    """Sorted parent IDs that have a ``cluster_<N>_hierarchical.csv`` on disk.

    Empty list if hierarchical refinement wasn't run (or the files were
    pruned). Caller uses this to decide which parents get a K spinner.
    """
    import glob

    out: list[int] = []
    for p in sorted(glob.glob(os.path.join(cluster_dir, "cluster_*_hierarchical.csv"))):
        m = _HIERARCHICAL_FILE_RE.search(os.path.basename(p))
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


@st.cache_data(show_spinner=False)
def _recut_hierarchical(parent_csv_path: str, k: int) -> pd.DataFrame:
    """Cut the parent's dendrogram into ``k`` sub-clusters; return K medoids.

    Recomputes ``scipy.cluster.hierarchy.linkage(X, method='ward')`` on the
    binary residue matrix from the CSV — same primitive PocketHunter uses
    internally, so the dendrogram is bit-identical to what produced the
    file's own ``hierarchical_cluster`` column. We flatten differently:
    ``fcluster(Z, t=k, criterion='maxclust')`` lets the caller choose
    granularity instead of being stuck with PocketHunter's hardcoded
    ``t=1.0`` distance cut (which over-splits — one real run produced 175
    singletons from a single parent).

    Cached by ``(path, k)`` because CSVs are immutable for the life of a
    job. ``k`` is silently clamped to ``len(rows)`` to keep callers simple.
    """
    import numpy as np
    from scipy.cluster.hierarchy import fcluster, linkage

    df = pd.read_csv(parent_csv_path)
    resid_cols = [c for c in df.columns if _RESIDUE_COL_RE.match(c)]
    if df.empty or not resid_cols:
        return df.iloc[0:0].copy()

    X = df[resid_cols].to_numpy(dtype=float)
    n = len(df)
    if n == 1 or k <= 1:
        return df.iloc[[0]].copy().reset_index(drop=True)

    k_eff = min(k, n)
    Z = linkage(X, method="ward")
    labels = fcluster(Z, t=k_eff, criterion="maxclust")

    medoid_rows = []
    for sub in sorted(set(int(s) for s in labels)):
        idxs = np.where(labels == sub)[0]
        local = _medoid_index(X[idxs])
        if local < 0:
            continue
        medoid_rows.append(df.iloc[int(idxs[local])])
    return pd.DataFrame(medoid_rows).reset_index(drop=True)


@st.cache_data(show_spinner=False)
def _load_cluster_csvs(reps_csv_path: str, clustered_csv_path: str, cluster_dir: str = ""):
    """Cached cluster-output CSV loader.

    Cluster job outputs are written once and immutable for the life of
    the job, so caching by full path string is safe — re-reads only
    happen when the path itself changes (different cluster job ID).
    Cache lives in-process; cleared by ``cleanup_job`` pruning the
    per-session results dir, or by a worker restart.

    Returns ``(df_reps, df_clustered_or_None, hier_parent_ids)``.
    ``df_clustered`` is pre-filtered to drop ``cluster == -1`` noise rows.
    ``hier_parent_ids`` is the sorted list of parent IDs that have a
    ``cluster_<N>_hierarchical.csv`` on disk — the Representatives tab uses
    it to decide which parents get a per-parent K spinner.
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
    hier_parent_ids = _hierarchical_parents(cluster_dir) if cluster_dir else []
    return df_reps, df_clustered, hier_parent_ids


def _cluster_pdb_sources(
    df_clustered: pd.DataFrame, pdb_source_job_id: str, cluster_id: int
) -> tuple[list[tuple, list[dict]]]:
    """Resolve the on-disk PDB paths + metadata rows for one cluster.

    Returns ``(pdb_sources, metadata_rows)`` ready for
    :func:`downloads.build_pocket_zip`. Missing files are silently
    omitted; ``build_pocket_zip`` does the final on-disk existence
    check during ZIP build.
    """
    from pathlib import Path

    members = df_clustered[df_clustered["cluster"] == cluster_id]
    pdbs_dir = Path(Config.RESULTS_DIR) / pdb_source_job_id / "pdbs"
    pdb_sources = []
    metadata_rows = []
    for _, row in members.iterrows():
        fname = str(row.get("File name", "") or "").strip()
        if not fname:
            continue
        # Strip p2rank's "_predictions" suffix and ensure ".pdb".
        if fname.endswith("_predictions"):
            fname = fname[: -len("_predictions")]
        if not fname.endswith(".pdb"):
            fname = f"{fname}.pdb"
        src = pdbs_dir / fname
        pdb_sources.append((src, fname))
        metadata_rows.append({
            "filename": fname,
            "cluster": cluster_id,
            "Frame": row.get("Frame", ""),
            "pocket_index": row.get("pocket_index", ""),
            "probability": row.get("probability", ""),
            "residues": row.get("residues", ""),
        })
    return pdb_sources, metadata_rows


def _render_single_cluster_zip_button(
    *,
    cluster_id: int,
    df_clustered: pd.DataFrame,
    pdb_source_job_id: str,
    safe_short: str,
    button_label: str,
) -> None:
    """Render an ``st.download_button`` for one cluster's full PDB set."""
    from downloads import MIME_ZIP, build_pocket_zip

    pdb_sources, metadata_rows = _cluster_pdb_sources(
        df_clustered, pdb_source_job_id, cluster_id
    )
    if not pdb_sources:
        st.info(f"Cluster {cluster_id} has no members on disk.")
        return
    zip_bytes, omitted = build_pocket_zip(
        pdb_sources, metadata_rows, cap_bytes=Config.MAX_DOWNLOAD_ZIP_SIZE,
    )
    st.download_button(
        label=button_label,
        data=zip_bytes,
        file_name=f"{safe_short}_cluster{cluster_id}_pockets.zip",
        mime=MIME_ZIP,
        use_container_width=True,
        key=f"cluster_dl_btn_{safe_short}_{cluster_id}",
    )
    if omitted:
        st.caption(
            f"⚠ {len(omitted)} file(s) omitted due to the "
            f"{Config.MAX_DOWNLOAD_ZIP_SIZE // (1024**2)} MB ZIP cap."
        )


def _render_cluster_members_download(
    *,
    selected_rows: pd.DataFrame,
    df_clustered: pd.DataFrame,
    pdb_source_job_id: str,
    safe_short: str,
) -> None:
    """Inline button in the Representatives tab: download all members
    of whichever clusters are currently selected in the reps table."""
    cluster_ids = sorted({
        int(c) for c in selected_rows.get("cluster", pd.Series(dtype=int))
        if pd.notna(c) and c != -1
    })
    if not cluster_ids:
        return
    if df_clustered is None or len(df_clustered) == 0 or not pdb_source_job_id:
        st.button(
            "↓ Download members",
            disabled=True,
            use_container_width=True,
            help="pockets_clustered.csv or source PDBs unavailable.",
        )
        return
    if len(cluster_ids) == 1:
        _render_single_cluster_zip_button(
            cluster_id=cluster_ids[0],
            df_clustered=df_clustered,
            pdb_source_job_id=pdb_source_job_id,
            safe_short=safe_short,
            button_label=f"↓ Download cluster {cluster_ids[0]} PDBs",
        )
    else:
        # Multi-cluster selection → bundle each cluster's PDBs in a
        # cluster_<N>/ folder.
        _render_multi_cluster_zip_button(
            cluster_ids=cluster_ids,
            df_clustered=df_clustered,
            pdb_source_job_id=pdb_source_job_id,
            safe_short=safe_short,
            button_label=f"↓ Download {len(cluster_ids)} clusters",
        )


def _render_multi_cluster_zip_button(
    *,
    cluster_ids: list[int],
    df_clustered: pd.DataFrame,
    pdb_source_job_id: str,
    safe_short: str,
    button_label: str,
) -> None:
    """ZIP-of-folders: each cluster gets its own ``cluster_N/`` folder
    inside one combined ZIP."""
    import io
    import zipfile
    from downloads import MIME_ZIP, _metadata_csv_bytes

    buf = io.BytesIO()
    total_running = 0
    omitted_total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as outer:
        for cid in cluster_ids:
            pdb_sources, metadata_rows = _cluster_pdb_sources(
                df_clustered, pdb_source_job_id, cid
            )
            if not pdb_sources:
                continue
            outer.writestr(
                f"cluster_{cid}/metadata.csv",
                _metadata_csv_bytes(metadata_rows),
            )
            # Smallest-first per cluster.
            entries = []
            for src, arc in pdb_sources:
                try:
                    entries.append((src, arc, src.stat().st_size))
                except OSError:
                    continue
            entries.sort(key=lambda e: e[2])
            for src, arc, size in entries:
                if total_running + size > Config.MAX_DOWNLOAD_ZIP_SIZE:
                    omitted_total += 1
                    continue
                outer.write(src, arcname=f"cluster_{cid}/{arc}")
                total_running += size
    st.download_button(
        label=button_label,
        data=buf.getvalue(),
        file_name=f"{safe_short}_clusters_{'-'.join(str(c) for c in cluster_ids)}.zip",
        mime=MIME_ZIP,
        use_container_width=True,
        key=f"cluster_dl_multi_{safe_short}_{'-'.join(str(c) for c in cluster_ids)}",
    )
    if omitted_total:
        st.caption(
            f"⚠ {omitted_total} file(s) omitted due to the "
            f"{Config.MAX_DOWNLOAD_ZIP_SIZE // (1024**2)} MB ZIP cap."
        )


def _render_all_clusters_zip_button(
    *,
    df_clustered: pd.DataFrame,
    pdb_source_job_id: str,
    safe_short: str,
) -> None:
    """ZIP-of-folders: every cluster in the run, one folder each."""
    cluster_ids = sorted(
        {int(c) for c in df_clustered["cluster"].dropna() if c != -1}
    )
    if not cluster_ids:
        return
    _render_multi_cluster_zip_button(
        cluster_ids=cluster_ids,
        df_clustered=df_clustered,
        pdb_source_job_id=pdb_source_job_id,
        safe_short=safe_short,
        button_label=f"↓ Download all {len(cluster_ids)} clusters (ZIP)",
    )


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

    disabled = (not is_editor) or chosen_legacy_id is None
    if not st.button(
        "Cluster",
        type="primary",
        use_container_width=True,
        disabled=disabled,
        key="cluster_submit",
    ):
        return

    _submit(chosen_legacy_id, min_prob, method, session)


def _submit(source_legacy_id: str, min_prob: float, method: str, session) -> None:
    # Phase C C4: refuse early on pool / per-session caps.
    sess_id = getattr(session, "id", None) if session else None
    try:
        assert_submit_allowed("fast", sess_id)
    except PoolCapHit as e:
        render_pool_cap_error(e)
        return

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
    # B3.4: forensic record. Defensive — failure is logged, not raised.
    from client_ip import client_ip
    from db import audit
    audit.record(sess_id, client_ip(), "cluster_submit",
                 {"job_id": job_id, "method": method, "min_prob": min_prob})
    # B11.10: clear the force-settings flag — fresh task is the
    # transition we held the settings view to enable.
    st.session_state.pop("cluster_force_settings", None)
    st.rerun()


@st.fragment(run_every="3s")
def _render_running_fragment() -> None:
    """Polls cluster task state every 3 s without re-rendering the whole page.

    Same shape as ``panels.find_pockets._render_running_fragment`` and
    ``panels.docking._docking_running_fragment``. The pre-fragment
    ``time.sleep(3); st.rerun()`` pattern caused whole-page DOM
    stacking — see the docstring on ``render_running_progress``.
    """
    task = _live_task_or_none()
    if task is None:
        st.rerun(scope="app")
        return
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
        st.rerun(scope="app")
        return
    render_running_progress(task, "Clustering pockets…", pool="fast")


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
        df_reps, df_clustered, hier_parent_ids = _load_cluster_csvs(
            reps_csv, clustered_csv, cluster_dir,
        )
    except Exception as e:
        st.error(
            f"Could not read cluster_representatives.csv: {e}\n\n"
            "The CSV may have been pruned by the cleanup job, or the "
            "underlying source job is gone. **Re-cluster** above to "
            "regenerate it, or pick a different source from the settings."
        )
        return

    # B2.3: zero-cluster diagnostic. DBSCAN can complete cleanly while
    # finding no clusters (probability threshold too high, stride too
    # large → too few pockets to form a density-connected set). Without
    # this callout the user sees an empty Representatives table with no
    # explanation.
    if df_reps is None or df_reps.empty:
        st.info(
            "**No clusters found.** DBSCAN returned zero clusters — "
            "either the per-pocket probability threshold (`min_prob`) "
            "filtered out everything, or there weren't enough pockets "
            "for the density-clustering to form a group.\n\n"
            "Try:\n"
            "- Lower **`min_prob`** in the settings (defaults to 0.5).\n"
            "- Re-run **Find Pockets** with a smaller stride to extract "
            "more frames.\n"
            "- Switch the method to **hierarchical** — it always "
            "produces clusters, at the cost of less-meaningful groupings."
        )
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

    # B11.2: resolve the *source* find_pockets job whose
    # ``pdbs/`` dir owns the per-frame PDBs. The cluster job has no
    # pdbs dir, so the heatmap's frame-jump lookup was failing
    # silently — see B11.2 plan, issue 2.
    from panels._shared import latest_job_of_kind

    source_job = latest_job_of_kind(session.id, ("find_pockets",))
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
    # Heatmap is the most informative first look at a finished cluster
    # run — the residue × cluster matrix shows the binding-pocket
    # footprints at a glance. Land users there by default and let them
    # drill into the table / reps tabs from there.
    st.session_state.setdefault(_active_tab_key, "Heatmap")
    active_tab = st.segmented_control(
        "Cluster view",
        options=_CLUSTER_TAB_OPTIONS,
        default=st.session_state[_active_tab_key],
        key=_active_tab_key,
        label_visibility="collapsed",
    )

    from db.sessions import safe_bidi_short
    safe_short = safe_bidi_short(session.short_code)

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

        # Surface the C5 auto-fallback (set by tasks.run_cluster_pockets_task
        # when hierarchical refinement crashed on single-member DBSCAN clusters
        # and the task re-ran without --hierarchical).
        result_info = latest_job.get("result_info") or {}
        if isinstance(result_info, dict) and result_info.get("hierarchical_fallback"):
            st.caption(
                "ℹ️ Hierarchical refinement was requested but skipped — "
                "your data had too few pockets per cluster to sub-divide "
                "meaningfully. DBSCAN-only result shown."
            )

        if "cluster" in df_reps.columns:
            parent_ids_in_reps = sorted(int(c) for c in df_reps["cluster"].dropna().unique())
        else:
            parent_ids_in_reps = []

        # Per-parent K spinners — only for parents that hierarchical refinement
        # produced sub-clusters for. Each spinner cuts THAT parent's dendrogram
        # into K sub-clusters (1..min(leaves, 10)); the table below re-renders
        # instantly from the cached recut.
        #
        # PocketHunter only emits a cluster_<N>_hierarchical.csv when N has
        # >1 member, so single-member parents (and parents from runs where
        # hierarchical was disabled or auto-skipped) have no spinner — they
        # always contribute exactly 1 rep. The caption below names them so
        # their absence from the spinner row doesn't look like a missing
        # control.
        parent_k: dict[int, int] = {}
        parent_max_k: dict[int, int] = {}
        single_only = [p for p in parent_ids_in_reps if p not in hier_parent_ids]
        if hier_parent_ids:
            st.markdown("**Representatives per parent cluster**")
            for pid in hier_parent_ids:
                pcsv = os.path.join(cluster_dir, f"cluster_{pid}_hierarchical.csv")
                try:
                    n_leaves = int(sum(1 for _ in open(pcsv)) - 1)
                except Exception:
                    n_leaves = 1
                parent_max_k[pid] = max(1, min(n_leaves, 10))
            ncols = min(4, len(hier_parent_ids))
            col_iter = st.columns(ncols)
            for i, pid in enumerate(hier_parent_ids):
                with col_iter[i % ncols]:
                    parent_k[pid] = int(st.number_input(
                        f"Cluster {pid}",
                        min_value=1,
                        max_value=parent_max_k[pid],
                        value=1,
                        step=1,
                        key=f"cluster_K_{results_job_id}_{pid}",
                        help=(
                            "Number of sub-cluster medoids to surface for this "
                            f"parent (1..{parent_max_k[pid]}). K=1 shows the "
                            "existing DBSCAN representative; K>1 re-cuts the "
                            "dendrogram at that granularity."
                        ),
                    ))
            if single_only:
                names = ", ".join(str(p) for p in single_only)
                st.caption(
                    f"Cluster{'s' if len(single_only) != 1 else ''} "
                    f"{names} {'have' if len(single_only) != 1 else 'has'} "
                    "only a single member — no sub-clusters to choose from, "
                    "always 1 representative."
                )

        # Build the unified display row-by-row. For each parent: K=1 keeps
        # the existing DBSCAN parent rep from cluster_representatives.csv
        # verbatim; K>=2 replaces it with K sub-cluster medoids from
        # re-cutting the dendrogram in the suite.
        rows: list[pd.Series] = []
        for pid in parent_ids_in_reps:
            parent_row = df_reps[df_reps["cluster"] == pid].iloc[0].copy()
            k = parent_k.get(pid, 1)
            pcsv = os.path.join(cluster_dir, f"cluster_{pid}_hierarchical.csv")

            if k <= 1 or pid not in hier_parent_ids or not os.path.exists(pcsv):
                parent_row["Cluster"] = str(pid)
                parent_row["_parent_cluster"] = pid
                parent_row["_sub_cluster"] = 0
                rows.append(parent_row)
                continue

            sub_df = _recut_hierarchical(pcsv, k)
            for j, (_, srow) in enumerate(sub_df.iterrows(), start=1):
                srow = srow.copy()
                srow["Cluster"] = f"  ↳ {pid}.{j}"
                srow["_parent_cluster"] = pid
                srow["_sub_cluster"] = j
                rows.append(srow)

        if rows:
            df_display = pd.DataFrame(rows).reset_index(drop=True)
            df_display = df_display.sort_values(
                ["_parent_cluster", "_sub_cluster"], kind="mergesort",
            ).reset_index(drop=True)
        else:
            # Fallback for the no-DBSCAN-cluster-column case (e.g. method=hierarchical).
            df_display = df_reps.copy().sort_values(
                "probability", ascending=False, kind="mergesort",
            ).reset_index(drop=True)
            if "Cluster" not in df_display.columns:
                df_display["Cluster"] = ""

        if any(parent_k.get(pid, 1) > 1 for pid in hier_parent_ids):
            st.caption(
                "Rows with `↳ X.Y` are sub-cluster medoids within DBSCAN "
                "cluster `X`, derived by cutting the dendrogram at the K you "
                "chose above. Adjust K to change granularity."
            )

        total_reps = len(df_display)
        if st.button(
            f"Add all {total_reps} cluster representatives → docking",
            use_container_width=True,
            type="primary",
            disabled=(not is_editor) or not pdb_source_job_id,
            help=(
                "One pocket per displayed row. Sub-cluster reps are grouped "
                "under their DBSCAN parent for the docking ensemble."
            ),
            key="cluster_add_reps",
        ):
            entries = []
            for _, row in df_display.iterrows():
                parent = row.get("_parent_cluster")
                cid = int(parent) if pd.notna(parent) else None
                entries.extend(docking_bucket.from_pockets_df(
                    pd.DataFrame([row]),
                    source_job_id=pdb_source_job_id,
                    cluster_override=cid,
                ))
            n_added = docking_bucket.add(entries)
            if n_added:
                st.success(f"Added {n_added} representative(s) to docking.")
            else:
                st.info("Cluster representatives already in the docking selection.")

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
        if "Cluster" not in df_display.columns:
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
            action_cols = st.columns([3, 2])
            with action_cols[0]:
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

            # Inline "Download all members of the selected cluster(s)"
            # button. Pulls full membership from df_clustered (the
            # source CSV that holds every pocket-row, not just reps).
            with action_cols[1]:
                _render_cluster_members_download(
                    selected_rows=df_display.iloc[reps_rows],
                    df_clustered=df_clustered,
                    pdb_source_job_id=pdb_source_job_id,
                    safe_short=safe_short,
                )

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

        # Bulk per-cluster PDB downloads — one ZIP per cluster, each
        # containing every member pocket's source-frame PDB plus a
        # ``metadata.csv``. ``df_clustered`` is the source-of-truth
        # for full membership (df_reps has one row per cluster).
        if df_clustered is not None and len(df_clustered) > 0 and pdb_source_job_id:
            st.divider()
            st.caption(
                "**Cluster PDBs** — per-frame PDBs for every member "
                "of a cluster, plus a metadata.csv."
            )
            cluster_ids_sorted = sorted(
                {int(c) for c in df_clustered["cluster"].dropna() if c != -1}
            )
            chosen_cluster = st.selectbox(
                "Cluster",
                options=cluster_ids_sorted,
                format_func=lambda c: f"Cluster {c}",
                key=f"cluster_dl_select_{safe_short}",
            )
            if chosen_cluster is not None:
                _render_single_cluster_zip_button(
                    cluster_id=int(chosen_cluster),
                    df_clustered=df_clustered,
                    pdb_source_job_id=pdb_source_job_id,
                    safe_short=safe_short,
                    button_label=f"↓ Download cluster {chosen_cluster} PDBs (ZIP)",
                )
            _render_all_clusters_zip_button(
                df_clustered=df_clustered,
                pdb_source_job_id=pdb_source_job_id,
                safe_short=safe_short,
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
        _render_running_fragment()
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
        # Phase D: a failed latest job used to fall through into
        # _render_results and produce the misleading "completed but
        # CSV missing" warning. Surface the actual failure instead.
        if str(latest.get("status", "")).lower() in ("failed", "failure"):
            _render_failed(latest, is_editor, session)
            return
        _render_results(latest, is_editor, session)
        return

    _render_settings(session, is_editor)


def _render_failed(latest_job: dict, is_editor: bool, session) -> None:
    """Render a structured failure panel for the most recent FAILED cluster job."""
    from failure_view import render_task_failure

    job_id = latest_job.get("legacy_id") or ""
    err = latest_job.get("error") if isinstance(latest_job.get("error"), dict) else {}
    # Build a synthetic task_info from the structured DB error so
    # render_task_failure's classify_error picks up exc_type/exc_message.
    task_info = {
        "exc_type": err.get("exc_type", "Exception"),
        "exc_message": err.get("exc_message") or latest_job.get("step") or "",
        "stage": err.get("stage", "cluster"),
        "status": latest_job.get("step") or "Cluster job failed.",
    }
    render_task_failure(task_info, status_json=None, job_id=job_id)

    # Friendly hint when the cause is the known PocketHunter hierarchical
    # crash (auto-fallback already tried + retry failed too, or some other
    # hierarchical-related failure).
    msg = str(task_info.get("exc_message", ""))
    if "empty distance matrix" in msg or "observations cannot be determined" in msg:
        st.info(
            "**Hint:** your input may have too few pockets per cluster "
            "for hierarchical refinement. Click **Re-cluster** above and "
            "uncheck _Hierarchical refinement_, OR lower **`min_prob`** "
            "to keep more pockets so DBSCAN finds multi-member clusters."
        )

    # Inline Re-cluster shortcut so the user can fix-and-retry in one click.
    if st.button(
        "Re-cluster (clear and choose new settings)",
        type="primary",
        use_container_width=True,
        disabled=not is_editor,
        key="cluster_rerun_from_failure",
    ):
        for k in ("cluster_task_id", "cluster_job_id", "cluster_status",
                  "cluster_view_job_id"):
            st.session_state.pop(k, None)
        st.session_state["cluster_force_settings"] = True
        st.rerun()
