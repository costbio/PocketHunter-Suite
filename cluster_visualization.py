"""Shared cluster-visualization helpers for the heatmap + 3D viewer.

The consensus heatmap, residue sort, layout pixel math, and the
protein-with-highlighted-pocket 3D viewer all live here. Used by
:mod:`panels.cluster` to render the cluster panel's results body.

The pure helpers (``residue_sort_key``, ``build_consensus_matrix``,
``filter_residue_columns``, ``sort_residues_in_matrix``,
``build_cluster_to_rep_mapping``, ``calculate_heatmap_layout``) have no
Streamlit imports and are unit-tested in
``tests/test_cluster_visualization.py``.
"""
from __future__ import annotations

from typing import Iterable, Optional

import numpy as np
import pandas as pd


# ── Pure helpers (no Streamlit) ──────────────────────────────────────────


def residue_sort_key(name: str) -> tuple[str, int]:
    """Sort key for residue strings like ``A_807`` → ``("A", 807)``.

    Falls back to ``(name, 0)`` for malformed input so ``sorted(..., key=...)``
    never crashes on data that doesn't match the expected ``chain_resi`` shape.
    """
    parts = name.rsplit("_", 1)
    try:
        return (parts[0], int(parts[1]))
    except (ValueError, IndexError):
        return (name, 0)


def build_consensus_matrix(
    df_clustered: pd.DataFrame,
    residue_cols: list[str],
    unique_clusters: list[int],
    df_reps: pd.DataFrame,
) -> tuple[np.ndarray, list[str]]:
    """Cluster-by-residue mean-occupancy matrix plus per-cluster label strings.

    Labels embed the cluster ID, the ``describe_cluster_spatially`` summary of
    the cluster's representative residues, the pocket count, and the average
    probability across the cluster's member pockets — matching the format
    the two pages have rendered since 5a02f82.
    """
    from cluster_labels import describe_cluster_spatially  # lazy import

    cluster_to_rep = build_cluster_to_rep_mapping(df_reps, df_clustered)

    rows: list[np.ndarray] = []
    labels: list[str] = []
    for clust in unique_clusters:
        clust_data = df_clustered[df_clustered["cluster"] == clust]
        rows.append(clust_data[residue_cols].mean().values)
        rep = cluster_to_rep.get(int(clust))
        spatial = describe_cluster_spatially(rep.get("residues") if rep is not None else None)
        labels.append(
            f"Cluster {clust} · {spatial}  "
            f"({len(clust_data)} pockets, avg prob: {clust_data['probability'].mean():.3f})"
        )
    return np.array(rows), labels


def filter_residue_columns(
    residues: list[str],
    matrix: np.ndarray,
) -> tuple[list[str], np.ndarray]:
    """Drop residue columns that are zero across every cluster row."""
    col_mask = matrix.sum(axis=0) > 0
    filtered_residues = [r for r, m in zip(residues, col_mask) if m]
    filtered_matrix = matrix[:, col_mask]
    return filtered_residues, filtered_matrix


def build_per_pocket_matrix(
    df_clustered: pd.DataFrame,
    residue_cols: list[str],
    *,
    insert_spacers: bool = True,
) -> tuple[np.ndarray, list[dict]]:
    """One row per pocket, grouped by cluster, p-sorted within cluster.

    Used by :func:`render_per_pocket_heatmap` to show every
    member pocket (not just the cluster representative) so within-cluster
    variation is visible. Pure — no Streamlit imports.

    Args:
        df_clustered: ``pockets_clustered.csv``-shaped DataFrame.
        residue_cols: Names of the residue-frequency columns. Must all
            be present in ``df_clustered``.
        insert_spacers: When ``True``, an all-zero spacer row is inserted
            between cluster groups so the heatmap renders a visible band
            gap. The spacer's ``row_meta`` entry has ``is_spacer=True``
            and no other keys.

    Returns:
        ``(matrix, row_meta)``. ``matrix`` has shape
        ``(n_pockets [+ spacers], len(residue_cols))``. ``row_meta`` is a
        list of dicts the same length as ``matrix``'s row count — either
        ``{"is_spacer": True}`` or
        ``{"is_spacer": False, "cluster": int, "probability": float,
           "file_name": str, "residues": str, "pocket_index": Any}``.
        Callers use ``row_meta`` to wire heatmap row clicks back to the
        Mol* viewer (frame + surface).
    """
    if df_clustered is None or len(df_clustered) == 0:
        return np.zeros((0, len(residue_cols))), []
    if "cluster" not in df_clustered.columns:
        return np.zeros((0, len(residue_cols))), []

    df = df_clustered.sort_values(
        ["cluster", "probability"],
        ascending=[True, False],
        kind="mergesort",  # stable so equal-prob rows keep insertion order
    )

    matrix_rows: list[np.ndarray] = []
    row_meta: list[dict] = []
    prev_cluster: Optional[int] = None

    for _, pocket in df.iterrows():
        try:
            cid = int(pocket["cluster"])
        except (TypeError, ValueError):
            continue
        if insert_spacers and prev_cluster is not None and cid != prev_cluster:
            matrix_rows.append(np.zeros(len(residue_cols)))
            row_meta.append({"is_spacer": True})

        matrix_rows.append(
            np.array([float(pocket.get(c, 0) or 0) for c in residue_cols])
        )
        prob_val = pocket.get("probability")
        try:
            prob = float(prob_val) if pd.notna(prob_val) else 0.0
        except (TypeError, ValueError):
            prob = 0.0
        row_meta.append({
            "is_spacer": False,
            "cluster": cid,
            "probability": prob,
            "file_name": str(pocket.get("File name", "") or ""),
            "residues": str(pocket.get("residues", "") or ""),
            "pocket_index": pocket.get("pocket_index"),
        })
        prev_cluster = cid

    if not matrix_rows:
        return np.zeros((0, len(residue_cols))), []
    return np.array(matrix_rows), row_meta


def sort_residues_in_matrix(
    residues: list[str],
    matrix: np.ndarray,
) -> tuple[list[str], np.ndarray]:
    """Reorder columns of ``matrix`` so the residue labels are numerically sorted."""
    if not residues:
        return list(residues), matrix
    order = sorted(range(len(residues)), key=lambda i: residue_sort_key(residues[i]))
    sorted_residues = [residues[i] for i in order]
    sorted_matrix = matrix[:, order]
    return sorted_residues, sorted_matrix


def build_cluster_to_rep_mapping(
    df_reps: pd.DataFrame,
    df_clustered: pd.DataFrame,
) -> dict[int, dict]:
    """Map cluster ID → representative row dict.

    Preferred path: there's a ``cluster`` column in ``df_reps`` and we key on it.
    Fallback: positional alignment between ``df_reps`` rows and the sorted
    unique clusters of ``df_clustered`` (matches the legacy behavior in both
    pages for CSVs missing the cluster column).
    """
    if "cluster" in df_reps.columns:
        return {int(row["cluster"]): row for _, row in df_reps.iterrows()}

    unique_clusters = sorted(df_clustered["cluster"].unique()) if "cluster" in df_clustered.columns else []
    return {
        clust: df_reps.iloc[i]
        for i, clust in enumerate(unique_clusters)
        if i < len(df_reps)
    }


def calculate_heatmap_layout(n_clusters: int) -> dict:
    """Pixel constants for the 3-column "checkboxes | heatmap | 3D viewer" layout.

    Identical math to the two pages' previous inline calculation: row height
    derived from the heatmap plot area, then a top pad + per-row gap so the
    Streamlit checkboxes line up vertically with the heatmap rows.
    """
    heat_top_margin = 60
    heat_bot_margin = 100
    height = max(400, n_clusters * 60 + 200)
    plot_area_h = height - heat_top_margin - heat_bot_margin
    row_h = plot_area_h / max(n_clusters, 1)
    cb_h = 36
    top_pad = max(0, heat_top_margin + row_h / 2 - cb_h / 2)
    gap = max(0, row_h - cb_h)
    return {
        "n_clust": n_clusters,
        "height": height,
        "heat_top_margin": heat_top_margin,
        "heat_bot_margin": heat_bot_margin,
        "row_h": row_h,
        "cb_h": cb_h,
        "top_pad": top_pad,
        "gap": gap,
    }


# ── Plotly figure builder (no Streamlit, returns a Figure) ───────────────


def build_consensus_heatmap_figure(
    matrix: np.ndarray,
    residues: list[str],
    cluster_labels: list[str],
    layout: dict,
):
    """Build the residue-frequency-per-cluster Plotly heatmap.

    Pure — returns a ``plotly.graph_objects.Figure``. Streamlit is not imported.
    """
    import plotly.graph_objects as go

    fig = go.Figure(
        data=go.Heatmap(
            z=matrix,
            x=residues,
            y=cluster_labels,
            colorscale="YlOrRd",
            zmin=0,
            zmax=1,
            colorbar=dict(title="Frequency", tickvals=[0, 0.25, 0.5, 0.75, 1.0]),
            hovertemplate=(
                "<b>%{y}</b><br>"
                "Residue: %{x}<br>"
                "Frequency: %{z:.2f}<extra></extra>"
            ),
        )
    )
    fig.update_layout(
        title="Residue Frequency per Cluster",
        xaxis_title="Residue",
        yaxis_title="",
        height=layout["height"],
        xaxis=dict(tickangle=45, tickfont=dict(size=9)),
        yaxis=dict(autorange="reversed", showticklabels=False),
        margin=dict(
            t=layout["heat_top_margin"],
            l=20,
            r=20,
            b=layout["heat_bot_margin"],
        ),
    )
    return fig


# ── Streamlit-coupled rendering ──────────────────────────────────────────


def show_pocket_3d(
    pdb_path: str,
    pocket_residues: Iterable[str],
    *,
    width: int = 800,
    height: int = 600,
) -> None:
    """Render a protein with the given pocket residues highlighted in orange.

    Imports streamlit + py3Dmol at call time so the pure helpers above stay
    importable without those heavyweights. Residue strings are ``chain_resi``
    (e.g. ``A_807``); anything that doesn't parse is silently skipped.
    """
    import py3Dmol
    import streamlit as st
    from streamlit.components.v1 import html as st_html

    try:
        with open(pdb_path, "r") as f:
            pdb_data = f.read()

        highlight_specs: list[dict] = []
        for res_str in pocket_residues:
            parts = res_str.strip().split("_", 1)
            if len(parts) == 2:
                try:
                    highlight_specs.append({"chain": parts[0], "resi": int(parts[1])})
                except ValueError:
                    pass

        view = py3Dmol.view(width=width, height=height)
        view.addModel(pdb_data, "pdb")
        view.setStyle({}, {"cartoon": {"color": "spectrum"}})
        for spec in highlight_specs:
            view.setStyle(
                {"chain": spec["chain"], "resi": spec["resi"]},
                {"stick": {"color": "orange", "radius": 0.3}},
            )
        if highlight_specs:
            chains: dict[str, list[int]] = {}
            for s in highlight_specs:
                chains.setdefault(s["chain"], []).append(s["resi"])
            for chain, resis in chains.items():
                view.addSurface(
                    py3Dmol.VDW,
                    {"opacity": 0.4, "color": "orange"},
                    {"chain": chain, "resi": resis},
                )
            view.zoomTo({"resi": [s["resi"] for s in highlight_specs]})
        else:
            view.zoomTo()
        view.spin(False)

        html = f'<div style="overflow:hidden;">{view._make_html()}</div>'
        st_html(html, height=height + 50, scrolling=False)
    except Exception as e:
        st.error(f"Error loading 3D structure: {e}")


def _resolve_rep_pdb_path(file_name: str, job_id: str) -> Optional[str]:
    """Locate a representative's PDB file. Mirrors the helper in docking_selection
    but specialised for the cluster pages — checks ``pdbs/`` then
    ``pocket_clusters/`` and accepts the p2rank ``_predictions`` suffix."""
    import os
    from config import Config

    pdb_name = file_name.replace("_predictions", "") if "_predictions" in file_name else file_name
    if not pdb_name.endswith(".pdb"):
        pdb_name += ".pdb"
    base = str(Config.RESULTS_DIR)
    for subdir in ("pdbs", "pocket_clusters"):
        cand = os.path.join(base, job_id, subdir, pdb_name)
        if os.path.exists(cand):
            return cand
    return None


def render_cluster_heatmap_wide(
    df_clustered: pd.DataFrame,
    df_reps: pd.DataFrame,
    results_job_id: str,
    *,
    key_prefix: str,
) -> None:
    """Wide cluster heatmap + docking selector for the v2 analysis app.

    Layout: a 2-column strip ``[cluster checkboxes | consensus heatmap]``
    using ``st.columns([1, 4])``. Designed to render full-width below
    the analysis panel (see ``analysis_app._render_cluster_heatmap_strip``);
    no internal 3D viewer column — the persistent Mol* viewer in the
    left column of the page covers 3D.

    Each cluster's checkbox is *the* "select this cluster for docking"
    affordance — toggling it mutates ``st.session_state.docking_target_clusters``
    directly (the key the docking panel reads via the heatmap-bridge).

    ``key_prefix`` namespaces the per-cluster widget keys so analysis_app
    can mount this multiple times (it currently mounts it once).
    """
    import streamlit as st

    from cluster_labels import describe_cluster_spatially

    if df_clustered is None or len(df_clustered) == 0:
        return

    # B9: the legacy ``session_state.initialize_session_state`` (deleted in
    # B7) seeded these keys at page load. The v2 panels don't, so do it
    # here defensively — first render no longer crashes on the bare reads.
    st.session_state.setdefault("docking_target_clusters", [])

    meta_cols = {
        "Frame_pocket_index", "File name", "Frame", "pocket_index",
        "probability", "residues", "cluster", "num_residues",
    }
    residue_cols = [c for c in df_clustered.columns if c not in meta_cols]
    if not residue_cols:
        st.warning("No residue columns found in clustered data.")
        return

    unique_clusters = sorted(df_clustered["cluster"].unique())
    cluster_to_rep = build_cluster_to_rep_mapping(df_reps, df_clustered)

    matrix, cluster_labels = build_consensus_matrix(
        df_clustered, residue_cols, unique_clusters, df_reps,
    )
    filtered_residues, filtered_matrix = filter_residue_columns(residue_cols, matrix)
    filtered_residues, filtered_matrix = sort_residues_in_matrix(filtered_residues, filtered_matrix)

    layout = calculate_heatmap_layout(len(unique_clusters))
    fig = build_consensus_heatmap_figure(filtered_matrix, filtered_residues, cluster_labels, layout)

    cb_col, heat_col = st.columns([1, 4])

    with cb_col:
        st.markdown("**Select for docking:**")
        st.markdown(f'<div style="height:{layout["top_pad"]:.0f}px"></div>', unsafe_allow_html=True)
        for cid in unique_clusters:
            rep = cluster_to_rep.get(cid)
            if rep is None:
                continue
            clust_df = df_clustered[df_clustered["cluster"] == cid]
            n_pockets = len(clust_df)
            avg_prob = clust_df["probability"].mean()
            spatial = describe_cluster_spatially(rep.get("residues") if rep is not None else None)

            checkbox_key = f"{key_prefix}_dock_{cid}"
            currently_selected = cid in st.session_state.docking_target_clusters

            sel = st.checkbox(
                f"Cluster {cid} · {spatial}",
                value=currently_selected,
                key=checkbox_key,
                help=f"{n_pockets} pockets · avg probability {avg_prob:.3f}",
            )
            if sel and cid not in st.session_state.docking_target_clusters:
                st.session_state.docking_target_clusters.append(cid)
            elif not sel and cid in st.session_state.docking_target_clusters:
                st.session_state.docking_target_clusters.remove(cid)

            st.markdown(f'<div style="height:{layout["gap"]:.0f}px"></div>', unsafe_allow_html=True)

    with heat_col:
        st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_consensus_heatmap")
        st.caption(
            "Each row = a cluster. Each column = a residue "
            "(labels are `chain_residueNumber` — e.g. `A_807` = chain A, residue 807). "
            "Color = how often that residue appears across the cluster's pockets "
            "(0 = never, 1 = always). "
            "**The representative pocket may not include every bright residue here** — "
            "the persistent Mol* viewer on the left shows the rep structure once a "
            "cluster is selected."
        )


def render_per_pocket_heatmap(
    df_clustered: pd.DataFrame,
    df_reps: pd.DataFrame,
    results_job_id: str,
    *,
    key_prefix: str,
    session_short: str,
    pdb_source_job_id: str,
) -> None:
    """B11.2: pockethunter-style per-cluster heatmap with click → viewer.

    Inspired by ``PocketHunter/pockethunter.py:plot_clustermap``: one
    matplotlib-equivalent subplot per cluster, each subplot drawn with
    that cluster's own light_palette colormap on the binary
    pocket-residue one-hot matrix. We adapt that to plotly via
    ``make_subplots(rows=n_clusters, cols=1, shared_xaxes=True)`` with
    per-row heights proportional to each cluster's pocket count.

    The colorscale is binary (white → cluster_color) — present residues
    paint in the cluster's hue, absent residues are blank. Each row's
    y-axis label is ``p={prob:.2f} · F={Frame}``. Subplot titles read
    ``Cluster 0``, ``Cluster 1``, …

    B11.20: clicking a row (no longer hover — too easy to trip while
    moving the mouse) pushes a single-pocket annotation into the Mol*
    viewer and jumps the frame slider to that pocket's source frame.

    Args:
        df_clustered: ``pockets_clustered.csv`` DataFrame (already
            filtered to drop ``cluster == -1`` noise rows).
        df_reps: ``cluster_representatives.csv`` DataFrame.
        results_job_id: cluster job's legacy_id — used only for keying
            the cache busts; the frame index lookup uses
            ``pdb_source_job_id``.
        key_prefix: namespace for widget keys.
        session_short: session ``short_code`` (already ``__``-sanitised).
        pdb_source_job_id: legacy_id of the find_pockets / pipeline job
            that produced the trajectory's per-frame PDBs. Required
            because ``frame_index_for_filename`` reads
            ``Config.RESULTS_DIR / <job_id> / pdbs/`` — and the
            *cluster* job has no such directory.
    """
    import streamlit as st
    from streamlit_plotly_events import plotly_events

    from components.molstar_annotations import _palette, merge_annotations
    from panels._shared import frame_index_for_filename

    if df_clustered is None or len(df_clustered) == 0:
        st.caption("No clustered pockets to visualise yet.")
        return
    if "cluster" not in df_clustered.columns:
        st.warning("`pockets_clustered.csv` missing a `cluster` column.")
        return

    meta_cols = {
        "Frame_pocket_index", "File name", "Frame", "pocket_index",
        "probability", "residues", "cluster", "num_residues",
    }
    residue_cols = [c for c in df_clustered.columns if c not in meta_cols]
    if not residue_cols:
        st.warning("No residue columns found in clustered data.")
        return

    # Drop residue columns that are zero across every pocket — keeps the
    # heatmap focused on residues that actually appear in at least one
    # pocket. Then numerically sort what's left so A_5 sits before A_50.
    raw_matrix = df_clustered[residue_cols].values
    col_mask = raw_matrix.sum(axis=0) > 0
    kept_residues = [r for r, m in zip(residue_cols, col_mask) if m]
    if not kept_residues:
        st.caption("No residues are occupied in any pocket — nothing to plot.")
        return
    sort_idx = sorted(
        range(len(kept_residues)),
        key=lambda i: residue_sort_key(kept_residues[i]),
    )
    sorted_residues = [kept_residues[i] for i in sort_idx]
    kept_cols = [c for c, m in zip(residue_cols, col_mask) if m]
    sorted_cols = [kept_cols[i] for i in sort_idx]

    # Group pockets by cluster (sorted ids); within each cluster, sort
    # by probability descending so the highest-confidence pocket sits
    # at the top of its subplot.
    cluster_ids = sorted(int(c) for c in df_clustered["cluster"].unique())
    palette = _palette(len(cluster_ids))
    cluster_color = {cid: color for cid, color in zip(cluster_ids, palette)}

    # Per-cluster matrices + row metadata (used to map hover events
    # back to specific pockets).
    cluster_matrices: dict[int, np.ndarray] = {}
    cluster_meta: dict[int, list[dict]] = {}
    for cid in cluster_ids:
        clust_df = df_clustered[df_clustered["cluster"] == cid].sort_values(
            "probability", ascending=False, kind="mergesort",
        )
        cluster_matrices[cid] = clust_df[sorted_cols].values.astype(float)
        meta_rows: list[dict] = []
        for _, pocket in clust_df.iterrows():
            try:
                prob = float(pocket.get("probability") or 0.0)
            except (TypeError, ValueError):
                prob = 0.0
            try:
                frame = int(pocket.get("Frame") or 0)
            except (TypeError, ValueError):
                frame = 0
            meta_rows.append({
                "cluster": cid,
                "probability": prob,
                "Frame": frame,
                "file_name": str(pocket.get("File name", "") or ""),
                "Frame_pocket_index": str(pocket.get("Frame_pocket_index", "") or ""),
                "pocket_index": pocket.get("pocket_index"),
                "residues": str(pocket.get("residues", "") or ""),
            })
        cluster_meta[cid] = meta_rows

    # Subplot heights: proportional to each cluster's pocket count, with
    # a floor so single-pocket clusters don't disappear. Total height
    # capped so the heatmap doesn't dominate the viewport.
    n_clusters = len(cluster_ids)
    total_pockets = sum(len(cluster_meta[c]) for c in cluster_ids)
    total_height = min(420, max(280, total_pockets * 16 + 40 * n_clusters))
    raw_heights = [max(1.0, float(len(cluster_meta[c]))) for c in cluster_ids]
    s = sum(raw_heights) or 1.0
    row_heights = [h / s for h in raw_heights]

    from plotly.subplots import make_subplots
    import plotly.graph_objects as go

    fig = make_subplots(
        rows=n_clusters, cols=1,
        shared_xaxes=True,
        row_heights=row_heights,
        vertical_spacing=0.04,
        subplot_titles=[f"Cluster {cid}" for cid in cluster_ids],
    )

    for idx, cid in enumerate(cluster_ids):
        m = cluster_matrices[cid]
        meta = cluster_meta[cid]
        color = cluster_color[cid]
        y_labels = [f"p={x['probability']:.2f} · F={x['Frame']}" for x in meta]
        fig.add_trace(
            go.Heatmap(
                z=m,
                x=sorted_residues,
                y=y_labels,
                # Binary colorscale: 0 transparent, 1 cluster's hue.
                colorscale=[[0, "rgba(255,255,255,0)"], [1, color]],
                zmin=0,
                zmax=1,
                showscale=False,
                hovertemplate=(
                    f"<b>Cluster {cid}</b><br>"
                    "%{y}<br>"
                    "Residue: %{x}<br>"
                    "<extra></extra>"
                ),
            ),
            row=idx + 1, col=1,
        )
        fig.update_yaxes(
            autorange="reversed",
            tickfont=dict(size=9),
            row=idx + 1, col=1,
        )

    fig.update_layout(
        height=total_height,
        margin=dict(t=30, l=20, r=20, b=70),
        showlegend=False,
        plot_bgcolor="white",
    )
    # X-axis labels only on the bottom subplot to save vertical space.
    for i in range(1, n_clusters):
        fig.update_xaxes(showticklabels=False, row=i, col=1)
    fig.update_xaxes(
        showticklabels=True, tickangle=45, tickfont=dict(size=8),
        title="Residue", row=n_clusters, col=1,
    )

    st.info(
        "👆 **Click** a heatmap row to preview that pocket in the viewer — "
        "moving the mouse over the heatmap alone does nothing.",
        icon="👆",
    )
    events = plotly_events(
        fig,
        click_event=True,
        hover_event=False,
        select_event=False,
        override_height=total_height,
        key=f"{key_prefix}_per_pocket_events",
    )

    st.caption(
        "**Click a row** to preview that pocket in the viewer (cluster "
        "color matches the Mol* overpaint). The viewer stays on the "
        "last-clicked pocket — use **Clear preview** to return to the "
        "cartoon-only view."
    )
    if st.button("Clear preview", key=f"{key_prefix}_clear_sel"):
        ann_key = f"viewer_annotations_{session_short}"
        ann_dict = st.session_state.setdefault(ann_key, {})
        if ann_dict.get("pockets") or ann_dict.get("focus"):
            merge_annotations(ann_dict, pockets=None, focus=None)
        st.session_state.pop(f"{key_prefix}_last_event_fp", None)
        st.rerun()

    if not events:
        return

    # plotly_events returns events with ``curveNumber`` (trace index) +
    # ``pointNumber`` ([row, col] for heatmaps). Each subplot is its own
    # trace, so curveNumber → cluster_ids[curveNumber].
    point = events[-1]
    curve = point.get("curveNumber")
    point_number = point.get("pointNumber")
    if point_number is None:
        point_number = point.get("pointIndex")

    row_idx: Optional[int] = None
    if isinstance(point_number, (list, tuple)) and point_number:
        try:
            row_idx = int(point_number[0])
        except (TypeError, ValueError):
            row_idx = None
    elif isinstance(point_number, int):
        ncols = max(1, len(sorted_residues))
        row_idx = point_number // ncols

    if curve is None or row_idx is None:
        return
    try:
        curve = int(curve)
    except (TypeError, ValueError):
        return
    if curve < 0 or curve >= n_clusters:
        return
    cid = cluster_ids[curve]
    meta_list = cluster_meta[cid]
    if row_idx < 0 or row_idx >= len(meta_list):
        return
    meta = meta_list[row_idx]

    color = cluster_color.get(cid, "#d4ff00")
    residue_list = [tok for tok in str(meta.get("residues", "")).split() if tok]
    if len(residue_list) < 3:
        return  # surface mesh degenerates with <3 residues

    fingerprint = (
        cid,
        str(meta.get("file_name", "")),
        str(meta.get("Frame_pocket_index", "")),
    )
    last_key = f"{key_prefix}_last_event_fp"
    if st.session_state.get(last_key) == fingerprint:
        return
    st.session_state[last_key] = fingerprint

    label = f"Cluster {cid} · p={meta['probability']:.2f}"
    ann_key = f"viewer_annotations_{session_short}"
    ann_dict = st.session_state.setdefault(ann_key, {})
    merge_annotations(
        ann_dict,
        pockets=[{
            "residues": residue_list,
            "color": color,
            "label": label,
        }],
        focus={"type": "pocket", "target": 0},
    )

    # B11.2: use the *source find_pockets* job id (which owns ``pdbs/``)
    # — NOT the cluster job's id. The cluster job has no pdbs directory,
    # so the prior call returned None and the viewer never jumped frames.
    target_frame = frame_index_for_filename(
        pdb_source_job_id, meta.get("file_name", "")
    )
    if target_frame is not None:
        target_key = f"viewer_frame_target_{session_short}"
        st.session_state[target_key] = int(target_frame)

    st.rerun()


def render_consensus_panel(
    df_clustered: pd.DataFrame,
    df_reps: pd.DataFrame,
    results_job_id: str,
    *,
    key_prefix: str,
    viewer_size: tuple[int, int] = (400, 420),  # noqa: ARG001 — kept for back-compat
) -> None:
    """Back-compat shim — see :func:`render_cluster_heatmap_wide`.

    The B9 layout drops the inline 3D viewer sub-column entirely, so the
    ``viewer_size`` argument is ignored. Existing callers keep working
    without code changes; new callers should call
    :func:`render_cluster_heatmap_wide` directly.
    """
    render_cluster_heatmap_wide(
        df_clustered, df_reps, results_job_id, key_prefix=key_prefix,
    )
