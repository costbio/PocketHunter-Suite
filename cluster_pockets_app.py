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
import py3Dmol
import streamlit.components.v1 as components
from pathlib import Path

# Use Config for directories
UPLOAD_DIR = str(Config.UPLOAD_DIR)
RESULTS_DIR = str(Config.RESULTS_DIR)

# Setup logging
logger = setup_logging(__name__)

# Custom CSS for enhanced UI
st.markdown("""
<style>
    .cluster-header {
        background: linear-gradient(135deg, #FFA726 0%, #FB8C00 50%, #EF6C00 100%);
        padding: 2rem;
        border-radius: 20px;
        margin-bottom: 2rem;
        color: white;
        text-align: center;
        box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        border: 1px solid rgba(255, 255, 255, 0.18);
    }

    .cluster-header h1 {
        margin: 0;
        font-size: 2.5rem;
        font-weight: 700;
        text-shadow: 2px 2px 4px rgba(0,0,0,0.1);
    }

    .cluster-card {
        background: rgba(255, 255, 255, 0.95);
        backdrop-filter: blur(10px);
        padding: 2rem;
        border-radius: 15px;
        box-shadow: 0 8px 32px rgba(31, 38, 135, 0.15);
        border: 1px solid rgba(255, 255, 255, 0.18);
        margin: 1.5rem 0;
        transition: all 0.3s ease;
    }

    .metric-card {
        background: linear-gradient(135deg, #FFF3E0 0%, #FFE0B2 100%);
        padding: 1.5rem;
        border-radius: 15px;
        text-align: center;
        box-shadow: 0 4px 15px rgba(0,0,0,0.1);
        transition: all 0.3s ease;
    }

    .metric-card:hover {
        transform: translateY(-5px);
        box-shadow: 0 8px 25px rgba(0,0,0,0.15);
    }

    .metric-value {
        font-size: 2rem;
        font-weight: 700;
        color: #F57C00;
        margin: 0.5rem 0;
    }

    .metric-label {
        font-size: 0.9rem;
        color: #666;
        text-transform: uppercase;
        letter-spacing: 1px;
    }

    .job-id-display {
        background: linear-gradient(135deg, #FFA726 0%, #FB8C00 100%);
        color: white;
        padding: 1.2rem;
        border-radius: 15px;
        font-family: 'Courier New', monospace;
        font-size: 1.1rem;
        text-align: center;
        margin: 1.5rem 0;
        box-shadow: 0 4px 20px rgba(255, 167, 38, 0.3);
        border: 1px solid rgba(255, 255, 255, 0.2);
    }

    .cluster-badge {
        display: inline-block;
        padding: 0.4rem 1rem;
        border-radius: 25px;
        font-size: 0.85rem;
        font-weight: 600;
        text-align: center;
        box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        background: linear-gradient(135deg, #FFA726 0%, #FB8C00 100%);
        color: white;
    }
</style>
""", unsafe_allow_html=True)

# Header
st.markdown("""
<div class="cluster-header">
    <h1>🎯 Step 3: Pocket Clustering</h1>
    <p style="font-size: 1.2rem; margin-top: 0.5rem;">Group similar pockets to identify representative binding sites</p>
</div>
""", unsafe_allow_html=True)

# Session state initialization
if 'cluster_job_id' not in st.session_state:
    st.session_state.cluster_job_id = None
if 'cluster_task_id' not in st.session_state:
    st.session_state.cluster_task_id = None
if 'cluster_status' not in st.session_state:
    st.session_state.cluster_status = 'idle'

# Initialize cached_job_ids if not exists
if 'cached_job_ids' not in st.session_state:
    st.session_state.cached_job_ids = {}

# Helper functions
def update_job_status(job_id, status, step=None, task_id=None, result_info=None):
    status_file = os.path.join(RESULTS_DIR, f'{job_id}_status.json')
    current_status = {}
    if os.path.exists(status_file):
        with open(status_file, 'r') as f:
            try:
                current_status = json.load(f)
            except json.JSONDecodeError:
                current_status = {}

    current_status['status'] = status
    if step:
        current_status['step'] = step
    if task_id:
        current_status['task_id'] = task_id
    if result_info:
        current_status['result_info'] = result_info
    current_status['last_updated'] = datetime.now().isoformat()

    with open(status_file, 'w') as f:
        json.dump(current_status, f, indent=4)

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
            st.error(f"❌ Clustering failed: {_task.info}")
            st.session_state.cluster_status = 'failed'
    except Exception as e:
        logger.error(f"Status banner error: {e}")

# ── Input Configuration ───────────────────────────────────────────────
st.markdown("### 📁 Input Configuration")

if st.session_state.cluster_job_id:
    st.markdown(f"""
    <div class="job-id-display">
        🔑 Current Job ID: {st.session_state.cluster_job_id}
    </div>
    """, unsafe_allow_html=True)

input_col1, input_col2 = st.columns(2)

with input_col1:
    st.markdown("**Option 1: Use Previous Step**")
    cached_detect_id = st.session_state.cached_job_ids.get('detect', '')
    detect_job_id = st.text_input(
        "Job ID from Step 2:",
        value=cached_detect_id,
        key="cluster_detect_job_id",
        help="Enter the Job ID from pocket detection"
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

# Run button
st.markdown("---")
if st.button("🚀 Start Pocket Clustering", type="primary", use_container_width=True):
    # Determine input source
    pockets_csv_path = None
    input_source = None

    if detect_job_id and detect_job_id.strip():
        detect_output_dir = os.path.join(RESULTS_DIR, detect_job_id.strip(), "pockets")
        potential_csv_path = os.path.join(detect_output_dir, "pockets.csv")
        if os.path.exists(potential_csv_path):
            pockets_csv_path = potential_csv_path
            input_source = f"Step 2 results (Job ID: {detect_job_id.strip()})"
        else:
            st.error(f"pockets.csv not found for Job ID: {detect_job_id.strip()}")
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

# ── Results ────────────────────────────────────────────────────────────
# Determine which job to show results for
results_job_id = st.session_state.cluster_job_id

# Allow loading previous results
with st.expander("📂 Load previous results"):
    load_job_id = st.text_input(
        "Enter Clustering Job ID:",
        value="",
        placeholder="e.g., cluster_20250815_143022_a1b2c3d4",
        key="cluster_load_job_id",
        help="Enter a clustering job ID to view its results"
    )
    if st.button("🔍 Load Results"):
        if load_job_id:
            st.session_state.cluster_job_id = load_job_id
            results_job_id = load_job_id
            st.rerun()

if results_job_id:
    cluster_output_dir = os.path.join(RESULTS_DIR, results_job_id, "pocket_clusters")
    representatives_file = os.path.join(cluster_output_dir, "cluster_representatives.csv")

    if os.path.exists(representatives_file):
        try:
            df_reps = pd.read_csv(representatives_file)

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

                # Overview metrics
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.markdown(f"""
                    <div class="metric-card">
                        <div class="metric-label">Total Clusters</div>
                        <div class="metric-value">{len(df_reps)}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col2:
                    st.markdown(f"""
                    <div class="metric-card">
                        <div class="metric-label">Avg Probability</div>
                        <div class="metric-value">{df_reps['probability'].mean():.3f}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col3:
                    st.markdown(f"""
                    <div class="metric-card">
                        <div class="metric-label">Avg Residues</div>
                        <div class="metric-value">{df_reps['num_residues'].mean():.0f}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col4:
                    st.markdown(f"""
                    <div class="metric-card">
                        <div class="metric-label">Best Probability</div>
                        <div class="metric-value">{df_reps['probability'].max():.3f}</div>
                    </div>
                    """, unsafe_allow_html=True)

                # Load full clustered pockets for heatmap (if available)
                clustered_file = os.path.join(cluster_output_dir, "pockets_clustered.csv")
                df_clustered = None
                if os.path.exists(clustered_file):
                    df_clustered = pd.read_csv(clustered_file)
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

                    st.dataframe(
                        df_display[['File name', 'probability', 'num_residues', 'Quality']],
                        use_container_width=True,
                        height=400
                    )

                    if len(df_display) > 0:
                        st.markdown("---")
                        st.markdown("**Select a cluster to view in 3D:**")
                        selected_idx = st.selectbox(
                            "Choose cluster:",
                            df_display.index,
                            format_func=lambda x: f"{df_display.loc[x, 'File name']} (Prob: {df_display.loc[x, 'probability']:.3f})"
                        )
                        if selected_idx is not None:
                            st.session_state.selected_cluster = df_display.loc[selected_idx].to_dict()

                with results_tab2:
                    if df_clustered is not None and len(df_clustered) > 0:
                        # Identify binary residue columns
                        meta_cols = {'Frame_pocket_index', 'File name', 'Frame', 'pocket_index',
                                     'probability', 'residues', 'cluster', 'num_residues'}
                        residue_cols = [c for c in df_clustered.columns if c not in meta_cols]

                        if residue_cols:
                            unique_clusters = sorted(df_clustered['cluster'].unique())

                            # --- Consensus Heatmap: residue frequency per cluster ---
                            consensus_rows = []
                            cluster_labels = []
                            for clust in unique_clusters:
                                clust_data = df_clustered[df_clustered['cluster'] == clust]
                                freq = clust_data[residue_cols].mean()
                                consensus_rows.append(freq.values)
                                cluster_labels.append(
                                    f"Cluster {clust}  ({len(clust_data)} pockets, avg prob: {clust_data['probability'].mean():.3f})"
                                )

                            consensus_matrix = np.array(consensus_rows)

                            # Drop residues that are never present in any cluster
                            col_mask = consensus_matrix.sum(axis=0) > 0
                            filtered_residues = [r for r, m in zip(residue_cols, col_mask) if m]
                            filtered_matrix = consensus_matrix[:, col_mask]

                            # Sort residues numerically (e.g. A_807 before A_1019)
                            def residue_sort_key(name):
                                parts = name.rsplit('_', 1)
                                try:
                                    return (parts[0], int(parts[1]))
                                except (ValueError, IndexError):
                                    return (name, 0)

                            sort_order = sorted(range(len(filtered_residues)),
                                                key=lambda i: residue_sort_key(filtered_residues[i]))
                            filtered_residues = [filtered_residues[i] for i in sort_order]
                            filtered_matrix = filtered_matrix[:, sort_order]

                            fig_heat = go.Figure(data=go.Heatmap(
                                z=filtered_matrix,
                                x=filtered_residues,
                                y=cluster_labels,
                                colorscale='YlOrRd',
                                zmin=0, zmax=1,
                                colorbar=dict(title="Frequency", tickvals=[0, 0.25, 0.5, 0.75, 1.0]),
                                hovertemplate=(
                                    "<b>%{y}</b><br>"
                                    "Residue: %{x}<br>"
                                    "Frequency: %{z:.2f}"
                                    "<extra></extra>"
                                ),
                            ))

                            height = max(400, len(unique_clusters) * 60 + 200)
                            fig_heat.update_layout(
                                title="Residue Frequency per Cluster",
                                xaxis_title="Residue",
                                yaxis_title="",
                                height=height,
                                xaxis=dict(tickangle=45, tickfont=dict(size=9)),
                                yaxis=dict(autorange="reversed"),
                                margin=dict(l=20, r=20, b=100),
                            )
                            st.plotly_chart(fig_heat, use_container_width=True)

                            st.markdown("""
                            **How to read this heatmap:**
                            - Each row is a cluster (binding site). Each column is a residue.
                            - Color intensity shows how consistently a residue appears across all pockets in that cluster (0 = never, 1 = always).
                            - **Core residues** (dark red, freq ~1.0) define the binding site. **Peripheral residues** (yellow/light) appear in some conformations only.
                            - Clusters with similar residue patterns target the same binding region; distinct patterns indicate different binding sites.
                            """)

                            # --- Per-pocket heatmap grouped by cluster ---
                            st.markdown("---")
                            st.markdown("#### Per-Pocket Residue Composition")

                            df_sorted = df_clustered.sort_values(['cluster', 'Frame'])
                            pocket_labels = [
                                f"C{int(row['cluster'])} | Frame {int(row['Frame'])} (p={row['probability']:.2f})"
                                for _, row in df_sorted.iterrows()
                            ]
                            pocket_matrix = df_sorted[filtered_residues].values

                            # Mark representative rows
                            rep_frames = set(df_reps['Frame_pocket_index'].values) if 'Frame_pocket_index' in df_reps.columns else set()
                            pocket_labels_marked = []
                            for i, (_, row) in enumerate(df_sorted.iterrows()):
                                fp_idx = row.get('Frame_pocket_index', '')
                                label = pocket_labels[i]
                                if fp_idx in rep_frames:
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

                        st.markdown(f"""
                        <div class="cluster-card">
                            <h4>🎯 Selected Cluster</h4>
                            <p><strong>File:</strong> {cluster.get('File name', 'N/A')}</p>
                            <p><strong>Probability:</strong> <span class="cluster-badge">
                                {cluster.get('probability', 0):.3f}
                            </span></p>
                            <p><strong>Residues:</strong> {cluster.get('num_residues', 0)}</p>
                        </div>
                        """, unsafe_allow_html=True)

                        viz_style = st.selectbox(
                            "Visualization Style:",
                            ["cartoon", "surface", "stick"],
                            help="Choose how to display the pocket structure"
                        )

                        pdb_filename = cluster.get('File name')
                        if pdb_filename:
                            pdb_path = os.path.join(cluster_output_dir, pdb_filename)
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
                            pdb_path = os.path.join(cluster_output_dir, pdb_filename)
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
                st.info("💡 Use the cluster representatives CSV in Step 4: Molecular Docking")

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
