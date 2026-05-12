"""Shared cluster-visualization helpers for the heatmap + 3D viewer.

Pipeline_app's ``_show_pipeline_cluster_inline`` and cluster_pockets_app's
``results_tab2`` previously carried near-identical implementations of the
consensus heatmap, residue sort, layout pixel math, and the
protein-with-highlighted-pocket 3D viewer. This module is the one source of
truth — both pages import the pure helpers + the ``render_consensus_panel``
Streamlit function.

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


def render_consensus_panel(
    df_clustered: pd.DataFrame,
    df_reps: pd.DataFrame,
    results_job_id: str,
    *,
    key_prefix: str,
    viewer_size: tuple[int, int] = (400, 420),
) -> None:
    """3-column heatmap panel shared by Step 2 (Cluster) and Full Pipeline.

    Renders ``[checkboxes | consensus heatmap | 3D viewer]``. Ticking a cluster
    checkbox updates ``st.session_state.cluster_preview_*`` so the 3D viewer
    shows that cluster's representative; toggling the "Select for Docking"
    checkbox inside the viewer panel mutates
    ``st.session_state.docking_target_clusters`` (the canonical key from
    7fd202d).

    ``key_prefix`` namespaces the per-cluster Streamlit widget keys so the
    two callers (cluster page + pipeline page) don't collide. Pass e.g.
    ``"cluster"`` from cluster_pockets_app and ``"pipe_cluster"`` from
    pipeline_app.
    """
    import os
    import streamlit as st

    from cluster_labels import describe_cluster_spatially

    if df_clustered is None or len(df_clustered) == 0:
        return

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

    cb_col, heat_col, viewer_col = st.columns([1, 3, 2])

    with cb_col:
        st.markdown("**Select cluster:**")
        st.markdown(f'<div style="height:{layout["top_pad"]:.0f}px"></div>', unsafe_allow_html=True)
        for cid in unique_clusters:
            rep = cluster_to_rep.get(cid)
            if rep is None:
                continue
            clust_df = df_clustered[df_clustered["cluster"] == cid]
            n_pockets = len(clust_df)
            avg_prob = clust_df["probability"].mean()

            def _on_change(_cid=cid, _rep=rep, _rj=results_job_id):
                cb_key = f"{key_prefix}_cb_{_cid}"
                if st.session_state[cb_key]:
                    pdb_path = _resolve_rep_pdb_path(str(_rep["File name"]), _rj)
                    res_raw = str(_rep.get("residues", ""))
                    res_list = [r.strip() for r in res_raw.replace(",", " ").split() if r.strip()]
                    st.session_state.cluster_preview_id = _cid
                    st.session_state.cluster_preview_pdb = pdb_path
                    st.session_state.cluster_preview_residues = res_list
                else:
                    if st.session_state.cluster_preview_id == _cid:
                        st.session_state.cluster_preview_id = None
                        st.session_state.cluster_preview_pdb = None
                        st.session_state.cluster_preview_residues = []

            spatial = describe_cluster_spatially(rep.get("residues") if rep is not None else None)
            st.checkbox(
                f"Cluster {cid} · {spatial}",
                key=f"{key_prefix}_cb_{cid}",
                on_change=_on_change,
                help=f"{n_pockets} pockets · avg probability {avg_prob:.3f}",
            )
            st.markdown(f'<div style="height:{layout["gap"]:.0f}px"></div>', unsafe_allow_html=True)

    with heat_col:
        st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_consensus_heatmap")
        st.caption(
            "Each row = a cluster. Each column = a residue "
            "(labels are `chain_residueNumber` — e.g. `A_807` = chain A, residue 807). "
            "Color = how often that residue appears across the cluster's pockets "
            "(0 = never, 1 = always). "
            "**The representative pocket may not include every bright residue here** — "
            "its exact residues are listed in the 3D viewer panel to the right."
        )

    with viewer_col:
        sel_id = st.session_state.cluster_preview_id
        if sel_id is not None:
            sel_path = st.session_state.cluster_preview_pdb
            sel_residues = st.session_state.cluster_preview_residues
            rep = cluster_to_rep.get(sel_id)
            spatial = describe_cluster_spatially(rep.get("residues") if rep is not None else None)
            st.markdown(f"**Cluster {sel_id}** · {spatial}")
            st.caption("Representative structure (medoid pocket)")
            if rep is not None:
                m1, m2 = st.columns(2)
                m1.metric("Probability", f"{rep.get('probability', 0):.3f}")
                m2.metric("Residues", len(sel_residues))
            is_selected = sel_id in st.session_state.docking_target_clusters
            if st.checkbox("Select for Docking", value=is_selected, key=f"{key_prefix}_dock_sel_{sel_id}"):
                if sel_id not in st.session_state.docking_target_clusters:
                    st.session_state.docking_target_clusters.append(sel_id)
            else:
                if sel_id in st.session_state.docking_target_clusters:
                    st.session_state.docking_target_clusters.remove(sel_id)
            if sel_path and os.path.exists(sel_path):
                show_pocket_3d(sel_path, sel_residues, width=viewer_size[0], height=viewer_size[1])
            else:
                st.warning(f"PDB not found: `{sel_path}`")
        else:
            st.info("← Check a cluster to view its 3D structure here")
