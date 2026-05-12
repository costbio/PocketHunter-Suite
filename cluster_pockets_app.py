import streamlit as st
import os
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import numpy as np
from datetime import datetime
import time
import json
import uuid
from tasks import run_cluster_pockets_task
from celery_app import celery_app
from config import Config
from security import handle_file_upload_secure, SecurityError
from rate_limiter import RateLimitExceeded, check_task_rate_limit
from logging_config import setup_logging
import re
import py3Dmol
import streamlit.components.v1 as components
from pathlib import Path
from session_state import initialize_session_state, render_load_previous_widget
from cluster_labels import describe_cluster_spatially

# Use Config for directories
UPLOAD_DIR = str(Config.UPLOAD_DIR)
RESULTS_DIR = str(Config.RESULTS_DIR)

# Setup logging
logger = setup_logging(__name__)

# Brutalist header — matches main.py .bh* pattern, no per-page CSS needed.
st.markdown("""
<div class="bh" style="margin-top: 4px;">
    <div class="bh-row">
        <span class="bh-title">Step 2 / Cluster Pockets</span>
        <span class="bh-version">[POCKETS → CLUSTERS]</span>
    </div>
    <div class="bh-rule"></div>
    <div class="bh-stages">
        Group similar pockets into representative binding sites
    </div>
</div>
""", unsafe_allow_html=True)

initialize_session_state()

# Shared status-file writer (canonical home: tasks.update_status_file).
from tasks import update_status_file as update_job_status

def resolve_pdb_path(file_name, job_id):
    """Resolve the actual PDB file path from a cluster representative filename.

    p2rank stores filenames as e.g. 'test_data_100.pdb_predictions'.
    The actual PDB lives in {job_id}/pdbs/test_data_100.pdb.
    Falls back to the pocket_clusters dir for standalone cluster runs.
    """
    # Strip p2rank suffix
    pdb_name = file_name.replace('_predictions', '') if '_predictions' in file_name else file_name
    if not pdb_name.endswith('.pdb'):
        pdb_name += '.pdb'
    # Primary: sibling pdbs/ directory (pipeline layout)
    candidate = os.path.join(RESULTS_DIR, job_id, 'pdbs', pdb_name)
    if os.path.exists(candidate):
        return candidate
    # Fallback: pocket_clusters dir (in case files were copied there)
    candidate2 = os.path.join(RESULTS_DIR, job_id, 'pocket_clusters', pdb_name)
    if os.path.exists(candidate2):
        return candidate2
    return candidate  # return primary even if missing (caller handles missing)


def show_molecule_3d(pdb_path, width=800, height=600, style="cartoon"):
    """Display 3D molecular structure using py3Dmol"""
    try:
        with open(pdb_path, 'r') as f:
            pdb_data = f.read()

        view = py3Dmol.view(width=width, height=height)
        view.addModel(pdb_data, 'pdb')

        if style == "cartoon":
            view.setStyle({'cartoon': {'color': 'spectrum'}})
        elif style == "surface":
            view.setStyle({'surface': {'opacity': 0.7, 'color': 'spectrum'}})
        elif style == "stick":
            view.setStyle({'stick': {'colorscheme': 'spectrum'}})

        view.zoomTo()
        view.spin(False)

        html = f"""
        <div style="border-radius: 15px; overflow: hidden; box-shadow: 0 8px 32px rgba(0,0,0,0.1);">
            {view._make_html()}
        </div>
        """
        components.html(html, height=height+50, scrolling=False)
    except Exception as e:
        st.error(f"Error loading 3D structure: {e}")
        logger.error(f"Error in show_molecule_3d: {e}", exc_info=True)


# The protein-with-highlighted-pocket viewer lives in cluster_visualization.
from cluster_visualization import show_pocket_3d as show_molecule_3d_with_pocket  # noqa: E402


# ── Status Banner ──────────────────────────────────────────────────────
if st.session_state.cluster_task_id:
    try:
        _task = celery_app.AsyncResult(st.session_state.cluster_task_id)
        if _task.state == 'PENDING':
            st.info("⏳ Task is pending in queue...")
            st.progress(0)
        elif _task.state == 'PROGRESS':
            _prog = (_task.info or {}).get('progress', 0)
            _step = (_task.info or {}).get('current_step', 'Processing...')
            st.info(f"🔄 {_step}")
            st.progress(_prog / 100)
        elif _task.state == 'SUCCESS':
            _result = _task.result or {}
            st.success(f"✅ Clustering completed! Clusters found: {_result.get('clusters_found', 'N/A')} | Time: {_result.get('processing_time', 0):.1f}s")
            st.progress(1.0)
            st.session_state.cluster_status = 'completed'
            st.session_state.cached_job_ids['cluster'] = st.session_state.cluster_job_id
        elif _task.state == 'FAILURE':
            from failure_view import load_status_json, render_task_failure
            _info = _task.info if isinstance(_task.info, dict) else {}
            render_task_failure(
                _info,
                load_status_json(st.session_state.cluster_job_id),
                st.session_state.cluster_job_id,
            )
            st.session_state.cluster_status = 'failed'
    except Exception as e:
        logger.error(f"Status banner error: {e}")

# ── Input Configuration ───────────────────────────────────────────────
st.markdown("### 📁 Input Configuration")

if st.session_state.cluster_job_id:
    st.caption(f"Current job: `{st.session_state.cluster_job_id}`")

input_col1, input_col2 = st.columns(2)

with input_col1:
    st.markdown("**Option 1: Use Previous Step**")
    cached_id = (
        st.session_state.cached_job_ids.get('find_pockets')
        or st.session_state.cached_job_ids.get('detect')  # legacy fallback
        or ''
    )
    detect_job_id = st.text_input(
        "Job ID from Step 1 (Find Pockets):",
        value=cached_id,
        key="cluster_detect_job_id",
        help="Enter the Job ID from Step 1 (or a legacy detect_* job ID)."
    )

with input_col2:
    st.markdown("**Option 2: Upload CSV**")
    pockets_csv = st.file_uploader(
        "Upload pockets.csv:",
        type=['csv'],
        key="cluster_pockets_csv",
        help="Upload pockets.csv from pocket detection"
    )

st.markdown("#### ⚙️ Clustering Parameters")

param_col1, param_col2 = st.columns(2)

with param_col1:
    min_prob = st.slider(
        "Min. Ligand-Binding Probability",
        min_value=0.0,
        max_value=1.0,
        value=0.5,
        step=0.05,
        key="cluster_min_prob",
        help="Minimum probability threshold for pocket clustering"
    )

with param_col2:
    clustering_method = st.selectbox(
        "Clustering Method",
        options=["dbscan", "hierarchical"],
        index=0,
        key="cluster_method",
        help="DBSCAN: Density-based | Hierarchical: Tree-based"
    )

# Advanced options
if clustering_method == "dbscan":
    dbscan_hierarchical = st.checkbox(
        "Enable Hierarchical Refinement",
        value=True,
        key="cluster_dbscan_hierarchical",
        help="Apply hierarchical sub-clustering within DBSCAN clusters"
    )
else:
    dbscan_hierarchical = False

with st.expander("ℹ️ About these parameters & what to do if clustering returns 0 clusters"):
    st.markdown(
        """
**`min_prob`** filters out pockets below this p2rank probability *before* clustering.
Higher → fewer, more confident pockets. Lower → more pockets, noisier signal.

**Clustering method:**
- **DBSCAN** — density-based; only groups dense regions, marks isolated pockets as
  *noise*. Its internal `epsilon` and `min_samples` are auto-tuned in the backend by
  silhouette score; they cannot be set manually from this UI.
- **Hierarchical** — tree-based; groups every pocket. No noise concept. Use this if
  DBSCAN keeps returning zero clusters.

**If you get 0 clusters (or "no representative pockets found"):**
1. **Lower `min_prob`** — try 0.3 or 0.2. The threshold may be too strict for this trajectory.
2. **Extract more frames** in Step 1 (lower the `stride` on Find Pockets). DBSCAN needs density to form clusters.
3. **Switch to Hierarchical** — it always produces clusters, even on sparse data.
        """.strip()
    )

# Run button
st.markdown("---")
if st.button("🚀 Start Pocket Clustering", type="primary", use_container_width=True):
    # Determine input source
    pockets_csv_path = None
    input_source = None

    if detect_job_id and detect_job_id.strip():
        from security import FileValidator, SecurityError
        try:
            safe_detect_id = FileValidator.validate_job_id(detect_job_id.strip())
        except SecurityError as e:
            st.error(f"Invalid Step 1 job ID: {e}")
            st.stop()
        detect_output_dir = os.path.join(RESULTS_DIR, safe_detect_id, "pockets")
        potential_csv_path = os.path.join(detect_output_dir, "pockets.csv")
        if os.path.exists(potential_csv_path):
            pockets_csv_path = potential_csv_path
            input_source = f"Step 1 (Find Pockets) results (Job ID: {safe_detect_id})"
        else:
            st.error(f"pockets.csv not found for Job ID: {safe_detect_id}")
            st.stop()

    elif pockets_csv:
        job_id = f"cluster_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        try:
            csv_path = handle_file_upload_secure(pockets_csv, job_id, "pockets_")
            logger.info(f"CSV file uploaded for job {job_id}")
        except SecurityError as e:
            st.error(f"❌ File upload failed: {e}")
            logger.error(f"Security error during CSV upload: {e}")
            st.stop()
        if csv_path:
            pockets_csv_path = csv_path
            input_source = "Uploaded pockets.csv"

    else:
        st.error("Please provide input using one of the options above.")
        st.stop()

    if pockets_csv_path:
        # Generate unique job ID
        job_id = f"cluster_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        st.session_state.cluster_job_id = job_id

        # Check task rate limit before submission
        try:
            check_task_rate_limit()
        except RateLimitExceeded as e:
            st.error(f"⏳ Task submission rate limit exceeded: {e}")
            st.info(f"Please wait {e.retry_after:.0f} seconds before submitting another task.")
            logger.warning(f"Task rate limit exceeded for job {job_id}: {e}")
            st.stop()

        # Update status
        update_job_status(job_id, 'submitted', 'Initializing pocket clustering')
        st.session_state.cluster_status = 'running'

        # Start the clustering
        with st.spinner("Starting pocket clustering..."):
            task = run_cluster_pockets_task.delay(
                pockets_csv_path_abs=os.path.abspath(pockets_csv_path),
                job_id=job_id,
                min_prob=min_prob,
                clustering_method=clustering_method,
                dbscan_hierarchical=dbscan_hierarchical
            )
            st.session_state.cluster_task_id = task.id
            update_job_status(job_id, 'running', 'Pocket clustering started', task_id=task.id)

        st.success(f"✅ Clustering started! Job ID: `{job_id}`")
        st.info(f"📂 Input: {input_source}")

@st.cache_data(ttl=300)
def load_clustered_data(path):
    return pd.read_csv(path)

@st.cache_data(ttl=300)
def load_representatives(path):
    return pd.read_csv(path)


# ── Results ────────────────────────────────────────────────────────────
# Determine which job to show results for
results_job_id = st.session_state.cluster_job_id


def _clear_heatmap_state(_safe_id):
    st.session_state.cluster_preview_id = None
    st.session_state.cluster_preview_pdb = None
    st.session_state.cluster_preview_residues = []
    st.session_state.docking_target_clusters = []


render_load_previous_widget(
    session_key="cluster_job_id",
    label="📂 Load previous results",
    placeholder="e.g., cluster_20250815_143022_a1b2c3d4",
    text_input_key="cluster_load_job_id",
    button_key="cluster_load_btn",
    on_load=_clear_heatmap_state,
)

if results_job_id:
    # Defense in depth: validate ID before any path-join uses it.
    from security import FileValidator, SecurityError
    try:
        results_job_id = FileValidator.validate_job_id(results_job_id)
    except SecurityError as e:
        st.error(f"Invalid job ID in session: {e}")
        st.stop()

    # Clear heatmap selection state whenever the active job changes
    if st.session_state.get('heatmap_last_job_id') != results_job_id:
        st.session_state.cluster_preview_id = None
        st.session_state.cluster_preview_pdb = None
        st.session_state.cluster_preview_residues = []
        st.session_state.docking_target_clusters = []
        st.session_state.heatmap_last_job_id = results_job_id

    cluster_output_dir = os.path.join(RESULTS_DIR, results_job_id, "pocket_clusters")
    representatives_file = os.path.join(cluster_output_dir, "cluster_representatives.csv")

    if os.path.exists(representatives_file):
        try:
            df_reps = load_representatives(representatives_file)

            # Compute numeric residue count from residue name strings
            if 'residues' in df_reps.columns and df_reps['residues'].dtype == object:
                df_reps['num_residues'] = df_reps['residues'].apply(
                    lambda x: len(str(x).split()) if pd.notna(x) else 0
                )
            elif 'residues' in df_reps.columns:
                df_reps['num_residues'] = df_reps['residues']
            else:
                df_reps['num_residues'] = 0

            if len(df_reps) == 0:
                st.warning("⚠️ Clustering completed but no representative pockets were found.")
            else:
                st.markdown("---")
                st.markdown("### 🎯 Clustering Results")

                with st.expander("ℹ️ Reading these results — what's a 'representative'?"):
                    st.markdown(
                        """
Each cluster groups pockets that touch a similar set of residues across
trajectory frames. The **representative** is the *single PDB frame* whose
pocket is closest (by Hamming distance on the residue-presence vector) to all
other pockets in that cluster — i.e. the most typical member, **not an
average structure**.

- The 3D Viewer and the docking step operate on this one representative frame.
- The **Residue Heatmap** shows residue *frequency across all pockets in the
  cluster*, so the representative's exact residues may not match the brightest
  spots on the heatmap.
- The full footprint of a cluster (the union of residues across every member
  pocket) can be larger than what the representative alone shows.
                        """.strip()
                    )

                # Overview metrics
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Total Clusters", len(df_reps))
                col2.metric("Avg Probability", f"{df_reps['probability'].mean():.3f}")
                col3.metric("Avg Residues", f"{df_reps['num_residues'].mean():.0f}")
                col4.metric("Best Probability", f"{df_reps['probability'].max():.3f}")

                # Load full clustered pockets for heatmap (if available)
                clustered_file = os.path.join(cluster_output_dir, "pockets_clustered.csv")
                df_clustered = None
                if os.path.exists(clustered_file):
                    df_clustered = load_clustered_data(clustered_file)
                    df_clustered = df_clustered[df_clustered['cluster'] != -1]

                # Sub-tabs for results data
                results_tab1, results_tab2, results_tab3, results_tab4, results_tab5 = st.tabs([
                    "📋 Cluster Table",
                    "🗺️ Residue Heatmap",
                    "📈 Distribution Analysis",
                    "🔬 3D Viewer",
                    "💾 Download"
                ])

                with results_tab1:
                    df_display = df_reps.sort_values('probability', ascending=False)

                    def get_quality_badge(prob):
                        if prob >= 0.8:
                            return "🟢 Excellent"
                        elif prob >= 0.6:
                            return "🟡 Good"
                        elif prob >= 0.4:
                            return "🟠 Moderate"
                        else:
                            return "🔴 Low"

                    df_display['Quality'] = df_display['probability'].apply(get_quality_badge)
                    if 'residues' in df_display.columns:
                        df_display['Location'] = df_display['residues'].apply(describe_cluster_spatially)
                    else:
                        df_display['Location'] = "—"

                    _table_cols = ['File name', 'Location', 'probability', 'num_residues', 'Quality']
                    if 'cluster' in df_display.columns:
                        df_display['Cluster'] = df_display['cluster'].astype('Int64')
                        _table_cols = ['Cluster'] + _table_cols
                    st.dataframe(
                        df_display[_table_cols],
                        use_container_width=True,
                        height=400
                    )

                    if len(df_display) > 0:
                        st.markdown("---")
                        st.markdown("**Preview a cluster in the 3D Viewer tab:**")
                        st.caption(
                            "This selection only drives the 3D Viewer tab below. "
                            "To pick clusters for **docking**, use the Residue Heatmap tab "
                            "and tick `Select for Docking` next to the cluster you want."
                        )

                        def _table_label(x):
                            _row = df_display.loc[x]
                            _cluster = _row.get('cluster', _row.get('Cluster', None))
                            _cluster_part = f"Cluster {int(_cluster)} · " if _cluster is not None and pd.notna(_cluster) else ""
                            return f"{_cluster_part}{_row['Location']} · Prob {_row['probability']:.3f}"

                        selected_idx = st.selectbox(
                            "Preview which cluster?",
                            df_display.index,
                            format_func=_table_label
                        )
                        if selected_idx is not None:
                            st.session_state.selected_cluster = df_display.loc[selected_idx].to_dict()

                with results_tab2:
                    if df_clustered is not None and len(df_clustered) > 0:
                        from cluster_visualization import (
                            build_consensus_matrix,
                            filter_residue_columns,
                            render_consensus_panel,
                            sort_residues_in_matrix,
                        )

                        # Shared 3-column consensus panel (heatmap + checkboxes + 3D viewer).
                        render_consensus_panel(
                            df_clustered, df_reps, results_job_id,
                            key_prefix="cluster",
                            viewer_size=(400, 420),
                        )

                        # The per-pocket heatmap below needs the same sorted, non-zero
                        # residue column subset. Recompute it via the pure helpers.
                        meta_cols = {'Frame_pocket_index', 'File name', 'Frame', 'pocket_index',
                                     'probability', 'residues', 'cluster', 'num_residues'}
                        residue_cols = [c for c in df_clustered.columns if c not in meta_cols]

                        if residue_cols:
                            unique_clusters = sorted(df_clustered['cluster'].unique())
                            consensus_matrix, _ = build_consensus_matrix(
                                df_clustered, residue_cols, unique_clusters, df_reps,
                            )
                            filtered_residues, _ = filter_residue_columns(residue_cols, consensus_matrix)
                            filtered_residues, _ = sort_residues_in_matrix(filtered_residues, consensus_matrix[:, [residue_cols.index(r) for r in filtered_residues]])

                            if st.session_state.docking_target_clusters:
                                selected_str = ", ".join(str(c) for c in st.session_state.docking_target_clusters)
                                st.info(f"Clusters selected for docking: **{selected_str}**")
                                if st.button("Start Docking with Selected Clusters", type="primary",
                                             use_container_width=True, key="heatmap_goto_docking"):
                                    st.session_state.cached_job_ids['cluster'] = results_job_id
                                    st.session_state.heatmap_preselected_for_docking = {
                                        'cluster_job_id': results_job_id,
                                        'cluster_ids': list(st.session_state.docking_target_clusters),
                                    }
                                    st.session_state.pending_nav = "Step 3: Molecular Docking"
                                    st.rerun()

                            # --- Per-pocket heatmap grouped by cluster ---
                            st.markdown("---")
                            st.markdown("#### Per-Pocket Residue Composition")

                            df_sorted = df_clustered.sort_values(['cluster', 'Frame'])
                            pocket_matrix = df_sorted[filtered_residues].values

                            # Mark representative rows
                            rep_frames = set(df_reps['Frame_pocket_index'].values) if 'Frame_pocket_index' in df_reps.columns else set()
                            pocket_labels_marked = []
                            for _, row in df_sorted.iterrows():
                                label = f"C{int(row['cluster'])} | Frame {int(row['Frame'])} (p={row['probability']:.2f})"
                                if row.get('Frame_pocket_index', '') in rep_frames:
                                    label = "★ " + label
                                pocket_labels_marked.append(label)

                            fig_detail = go.Figure(data=go.Heatmap(
                                z=pocket_matrix,
                                x=filtered_residues,
                                y=pocket_labels_marked,
                                colorscale=[[0, '#FFF3E0'], [1, '#E65100']],
                                zmin=0, zmax=1,
                                showscale=False,
                                hovertemplate=(
                                    "<b>%{y}</b><br>"
                                    "Residue: %{x}<br>"
                                    "Present: %{z}"
                                    "<extra></extra>"
                                ),
                            ))

                            detail_height = max(400, len(df_sorted) * 30 + 200)
                            fig_detail.update_layout(
                                title="Individual Pocket Residue Composition (★ = representative)",
                                xaxis_title="Residue",
                                yaxis_title="",
                                height=detail_height,
                                xaxis=dict(tickangle=45, tickfont=dict(size=9)),
                                yaxis=dict(autorange="reversed", tickfont=dict(size=10)),
                                margin=dict(l=20, r=20, b=100),
                            )
                            st.plotly_chart(fig_detail, use_container_width=True)
                            st.caption(
                                "One row per individual pocket. Residue labels are `chain_residueNumber` "
                                "(e.g. `A_807` = chain A, residue 807). "
                                "★ marks the medoid frame that was elected representative for each cluster."
                            )
                        else:
                            st.warning("No residue columns found in clustered data.")
                    else:
                        st.info("Heatmap requires pockets_clustered.csv which was not found for this job.")

                with results_tab3:
                    col1, col2 = st.columns(2)

                    with col1:
                        fig_hist = px.histogram(
                            df_reps,
                            x='probability',
                            title='Probability Distribution',
                            nbins=20,
                            color_discrete_sequence=['#FFA726']
                        )
                        fig_hist.update_layout(
                            xaxis_title="Binding Probability",
                            yaxis_title="Number of Clusters",
                            showlegend=False
                        )
                        st.plotly_chart(fig_hist, use_container_width=True)

                    with col2:
                        fig_residues = px.box(
                            df_reps,
                            y='num_residues',
                            title='Residue Count Distribution',
                            color_discrete_sequence=['#FB8C00']
                        )
                        fig_residues.update_layout(
                            yaxis_title="Number of Residues",
                            showlegend=False
                        )
                        st.plotly_chart(fig_residues, use_container_width=True)

                    fig_scatter = px.scatter(
                        df_reps,
                        x='num_residues',
                        y='probability',
                        size='probability',
                        color='probability',
                        title='Cluster Probability vs Pocket Size',
                        labels={'num_residues': 'Number of Residues', 'probability': 'Binding Probability'},
                        color_continuous_scale='Oranges',
                        hover_data=['File name']
                    )
                    fig_scatter.update_traces(marker=dict(line=dict(width=1, color='DarkOrange')))
                    st.plotly_chart(fig_scatter, use_container_width=True)

                    stats_col1, stats_col2, stats_col3, stats_col4 = st.columns(4)
                    with stats_col1:
                        st.metric("Mean Probability", f"{df_reps['probability'].mean():.3f}")
                    with stats_col2:
                        st.metric("Median Probability", f"{df_reps['probability'].median():.3f}")
                    with stats_col3:
                        st.metric("Std Dev", f"{df_reps['probability'].std():.3f}")
                    with stats_col4:
                        high_quality = len(df_reps[df_reps['probability'] >= 0.7])
                        st.metric("High Quality (>=0.7)", high_quality)

                with results_tab4:
                    if 'selected_cluster' in st.session_state and st.session_state.selected_cluster:
                        cluster = st.session_state.selected_cluster

                        st.markdown("**Selected Cluster**")
                        sc1, sc2, sc3 = st.columns(3)
                        sc1.write(f"**File:** `{cluster.get('File name', 'N/A')}`")
                        sc2.metric("Probability", f"{cluster.get('probability', 0):.3f}")
                        sc3.metric("Residues", cluster.get('num_residues', 0))

                        viz_style = st.selectbox(
                            "Visualization Style:",
                            ["cartoon", "surface", "stick"],
                            help="Choose how to display the pocket structure"
                        )

                        pdb_filename = cluster.get('File name')
                        if pdb_filename:
                            pdb_path = resolve_pdb_path(pdb_filename, results_job_id)
                            if os.path.exists(pdb_path):
                                show_molecule_3d(pdb_path, style=viz_style)
                            else:
                                st.warning(f"⚠️ PDB file not found: {pdb_path}")
                    else:
                        st.info("ℹ️ Select a cluster from the Cluster Table tab to view it in 3D.")

                        if len(df_reps) > 0:
                            st.markdown("#### 📺 Preview: First Cluster")
                            first_cluster = df_reps.iloc[0]
                            pdb_filename = first_cluster['File name']
                            pdb_path = resolve_pdb_path(pdb_filename, results_job_id)
                            if os.path.exists(pdb_path):
                                show_molecule_3d(pdb_path, width=600, height=400)

                with results_tab5:
                    col1, col2 = st.columns(2)

                    with col1:
                        st.markdown("**📄 Data Files**")
                        csv_data = df_reps.to_csv(index=False)
                        st.download_button(
                            label="📥 Download Cluster Representatives (CSV)",
                            data=csv_data,
                            file_name=f"cluster_representatives_{results_job_id}.csv",
                            mime="text/csv",
                            use_container_width=True
                        )

                        df_high_quality = df_reps[df_reps['probability'] >= 0.7]
                        if len(df_high_quality) > 0:
                            hq_csv = df_high_quality.to_csv(index=False)
                            st.download_button(
                                label="📥 Download High Quality Clusters (CSV)",
                                data=hq_csv,
                                file_name=f"high_quality_clusters_{results_job_id}.csv",
                                mime="text/csv",
                                use_container_width=True
                            )

                    with col2:
                        st.markdown("**📦 Structure Files**")
                        if st.button("🔄 Generate PDB Archive", use_container_width=True):
                            import zipfile
                            with st.spinner("Creating archive..."):
                                zip_path = os.path.join(cluster_output_dir, 'cluster_structures.zip')
                                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                                    for pdb_file in Path(cluster_output_dir).glob('*.pdb'):
                                        zipf.write(pdb_file, pdb_file.name)
                                st.success("✅ Archive created!")

                        zip_path = os.path.join(cluster_output_dir, 'cluster_structures.zip')
                        if os.path.exists(zip_path):
                            with open(zip_path, 'rb') as f:
                                st.download_button(
                                    label="📥 Download All PDB Files (ZIP)",
                                    data=f.read(),
                                    file_name=f"cluster_structures_{results_job_id}.zip",
                                    mime="application/zip",
                                    use_container_width=True
                                )

                st.markdown("---")
                st.info("💡 Use the cluster representatives CSV in Step 3: Molecular Docking")

        except Exception as e:
            st.error(f"Error loading results: {e}")
            logger.error(f"Results loading error: {e}", exc_info=True)

# Auto-refresh when task is running
if st.session_state.cluster_status == 'running' and st.session_state.cluster_task_id:
    try:
        task = celery_app.AsyncResult(st.session_state.cluster_task_id)
        if task.ready():
            st.session_state.cluster_status = 'completed'
            st.rerun()
        else:
            time.sleep(3)
            st.rerun()
    except Exception:
        time.sleep(3)
        st.rerun()

# Footer
st.markdown("---")
st.markdown("""
<div style='text-align: center; color: #666;'>
    <p>🎯 Pocket Clustering | Part of the PocketHunter Suite</p>
    <p style='font-size: 0.85rem; margin-top: 0.5rem;'>
        💡 Tip: High-probability clusters (>=0.7) are recommended for docking studies
    </p>
</div>
""", unsafe_allow_html=True)
