import streamlit as st
import os
import uuid
import json
from datetime import datetime
import time
from pathlib import Path
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import py3Dmol
import streamlit.components.v1 as components
from tasks import run_pockethunter_pipeline
from celery_app import celery_app
from config import Config
from security import handle_file_upload_secure, SecurityError
from rate_limiter import RateLimitExceeded, check_task_rate_limit
from logging_config import setup_logging
from session_state import initialize_session_state

UPLOAD_DIR = str(Config.UPLOAD_DIR)
RESULTS_DIR = str(Config.RESULTS_DIR)

logger = setup_logging(__name__)

# Custom CSS
st.markdown("""
<style>
    .pipeline-header {
        background: linear-gradient(135deg, #2E7D32 0%, #1565C0 50%, #F57C00 100%);
        padding: 2rem;
        border-radius: 20px;
        margin-bottom: 2rem;
        color: white;
        text-align: center;
        box-shadow: 0 8px 32px rgba(0,0,0,0.1);
    }
    .pipeline-header h1 { margin: 0; font-size: 2.2rem; font-weight: 700; }
    .stage-indicator {
        display: flex;
        justify-content: space-between;
        margin: 1.5rem 0;
    }
    .stage-box {
        flex: 1;
        text-align: center;
        padding: 0.8rem;
        border-radius: 10px;
        margin: 0 4px;
        font-size: 0.85rem;
        font-weight: 600;
    }
    .stage-done { background: #C8E6C9; color: #1B5E20; }
    .stage-active { background: #1565C0; color: white; }
    .stage-pending { background: #E0E0E0; color: #757575; }
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="pipeline-header">
    <h1>⚡ Full Pipeline</h1>
    <p style="font-size: 1.1rem; margin-top: 0.5rem;">
        Extract → Detect → Cluster — all in one shot
    </p>
</div>
""", unsafe_allow_html=True)

# ── Cluster results helpers ─────────────────────────────────────────────

def _resolve_pdb_path(file_name, job_id):
    pdb_name = file_name.replace('_predictions', '') if '_predictions' in file_name else file_name
    if not pdb_name.endswith('.pdb'):
        pdb_name += '.pdb'
    candidate = os.path.join(RESULTS_DIR, job_id, 'pdbs', pdb_name)
    if os.path.exists(candidate):
        return candidate
    candidate2 = os.path.join(RESULTS_DIR, job_id, 'pocket_clusters', pdb_name)
    if os.path.exists(candidate2):
        return candidate2
    return candidate


def _show_molecule_3d_with_pocket(pdb_path, pocket_residues, width=400, height=420):
    try:
        with open(pdb_path, 'r') as f:
            pdb_data = f.read()
        highlight_specs = []
        for res_str in pocket_residues:
            parts = res_str.strip().split('_', 1)
            if len(parts) == 2:
                try:
                    highlight_specs.append({'chain': parts[0], 'resi': int(parts[1])})
                except ValueError:
                    pass
        view = py3Dmol.view(width=width, height=height)
        view.addModel(pdb_data, 'pdb')
        view.setStyle({}, {'cartoon': {'color': 'spectrum'}})
        for spec in highlight_specs:
            view.setStyle({'chain': spec['chain'], 'resi': spec['resi']},
                          {'stick': {'color': 'orange', 'radius': 0.3}})
        if highlight_specs:
            chains = {}
            for s in highlight_specs:
                chains.setdefault(s['chain'], []).append(s['resi'])
            for chain, resis in chains.items():
                view.addSurface(py3Dmol.VDW, {'opacity': 0.4, 'color': 'orange'},
                                {'chain': chain, 'resi': resis})
            view.zoomTo({'resi': [s['resi'] for s in highlight_specs]})
        else:
            view.zoomTo()
        view.spin(False)
        html = f'<div style="border-radius:15px;overflow:hidden;">{view._make_html()}</div>'
        components.html(html, height=height + 50, scrolling=False)
    except Exception as e:
        st.error(f"Error loading 3D structure: {e}")


@st.cache_data(ttl=300)
def _load_clustered_data(path):
    return pd.read_csv(path)


@st.cache_data(ttl=300)
def _load_representatives(path):
    return pd.read_csv(path)




def _show_pipeline_cluster_inline(results_job_id):
    """Render the heatmap + 3D viewer + docking launch inline on the pipeline page."""
    cluster_output_dir = os.path.join(RESULTS_DIR, results_job_id, "pocket_clusters")
    representatives_file = os.path.join(cluster_output_dir, "cluster_representatives.csv")

    if not os.path.exists(representatives_file):
        st.info("Cluster results not yet available.")
        return

    # Clear heatmap state when job changes
    if st.session_state.get('heatmap_last_job_id') != results_job_id:
        st.session_state.heatmap_selected_cluster_id = None
        st.session_state.heatmap_selected_pdb_path = None
        st.session_state.heatmap_selected_residues = []
        st.session_state.heatmap_last_job_id = results_job_id

    try:
        df_reps = _load_representatives(representatives_file)
        if 'residues' in df_reps.columns and df_reps['residues'].dtype == object:
            df_reps['num_residues'] = df_reps['residues'].apply(
                lambda x: len(str(x).split()) if pd.notna(x) else 0
            )
        else:
            df_reps['num_residues'] = df_reps.get('residues', 0)

        if len(df_reps) == 0:
            st.warning("⚠️ Clustering completed but no representative pockets found.")
            return

        st.markdown("---")
        st.markdown("### 🗺️ Clustering Results")

        clustered_file = os.path.join(cluster_output_dir, "pockets_clustered.csv")
        if not os.path.exists(clustered_file):
            st.info("Heatmap requires pockets_clustered.csv — not found for this job.")
            return

        df_clustered = _load_clustered_data(clustered_file)
        df_clustered = df_clustered[df_clustered['cluster'] != -1]

        meta_cols = {'Frame_pocket_index', 'File name', 'Frame', 'pocket_index',
                     'probability', 'residues', 'cluster', 'num_residues'}
        residue_cols = [c for c in df_clustered.columns if c not in meta_cols]
        if not residue_cols:
            st.warning("No residue columns found in clustered data.")
            return

        unique_clusters = sorted(df_clustered['cluster'].unique())

        if 'cluster' in df_reps.columns:
            cluster_to_rep = {int(row['cluster']): row for _, row in df_reps.iterrows()}
        else:
            cluster_to_rep = {
                clust: df_reps.iloc[i]
                for i, clust in enumerate(unique_clusters)
                if i < len(df_reps)
            }

        # Build consensus heatmap matrix
        consensus_rows, cluster_labels = [], []
        for clust in unique_clusters:
            clust_data = df_clustered[df_clustered['cluster'] == clust]
            consensus_rows.append(clust_data[residue_cols].mean().values)
            cluster_labels.append(
                f"Cluster {clust}  ({len(clust_data)} pockets, avg prob: {clust_data['probability'].mean():.3f})"
            )
        consensus_matrix = np.array(consensus_rows)
        col_mask = consensus_matrix.sum(axis=0) > 0
        filtered_residues = [r for r, m in zip(residue_cols, col_mask) if m]
        filtered_matrix = consensus_matrix[:, col_mask]

        def _res_sort_key(name):
            parts = name.rsplit('_', 1)
            try:
                return (parts[0], int(parts[1]))
            except (ValueError, IndexError):
                return (name, 0)

        sort_order = sorted(range(len(filtered_residues)), key=lambda i: _res_sort_key(filtered_residues[i]))
        filtered_residues = [filtered_residues[i] for i in sort_order]
        filtered_matrix = filtered_matrix[:, sort_order]

        _n_clust = len(unique_clusters)
        _heat_top_margin = 60
        _heat_bot_margin = 100
        height = max(400, _n_clust * 60 + 200)
        _plot_area_h = height - _heat_top_margin - _heat_bot_margin
        _row_h = _plot_area_h / _n_clust
        _cb_h = 36
        _top_pad = max(0, _heat_top_margin + _row_h / 2 - _cb_h / 2)
        _gap = max(0, _row_h - _cb_h)

        fig_heat = go.Figure(data=go.Heatmap(
            z=filtered_matrix,
            x=filtered_residues,
            y=cluster_labels,
            colorscale='YlOrRd',
            zmin=0, zmax=1,
            colorbar=dict(title="Frequency", tickvals=[0, 0.25, 0.5, 0.75, 1.0]),
            hovertemplate="<b>%{y}</b><br>Residue: %{x}<br>Frequency: %{z:.2f}<extra></extra>",
        ))
        fig_heat.update_layout(
            title="Residue Frequency per Cluster",
            xaxis_title="Residue",
            yaxis_title="",
            height=height,
            xaxis=dict(tickangle=45, tickfont=dict(size=9)),
            yaxis=dict(autorange="reversed", showticklabels=False),
            margin=dict(t=_heat_top_margin, l=20, r=20, b=_heat_bot_margin),
        )

        cb_col, heat_col, viewer_col = st.columns([1, 3, 2])

        with cb_col:
            st.markdown("**Select cluster:**")
            st.markdown(f'<div style="height:{_top_pad:.0f}px"></div>', unsafe_allow_html=True)
            for _cid in unique_clusters:
                _rep = cluster_to_rep.get(_cid)
                if _rep is None:
                    continue
                _clust_df = df_clustered[df_clustered['cluster'] == _cid]
                _n = len(_clust_df)
                _avg = _clust_df['probability'].mean()

                def _on_change(_cid=_cid, _rep=_rep, _rj=results_job_id):
                    cb_key = f"pipe_cluster_cb_{_cid}"
                    if st.session_state[cb_key]:
                        _pdb_path = _resolve_pdb_path(_rep['File name'], _rj)
                        _res_raw = str(_rep.get('residues', ''))
                        _res_list = [r.strip() for r in _res_raw.replace(',', ' ').split() if r.strip()]
                        st.session_state.heatmap_selected_cluster_id = _cid
                        st.session_state.heatmap_selected_pdb_path = _pdb_path
                        st.session_state.heatmap_selected_residues = _res_list
                    else:
                        if st.session_state.heatmap_selected_cluster_id == _cid:
                            st.session_state.heatmap_selected_cluster_id = None
                            st.session_state.heatmap_selected_pdb_path = None
                            st.session_state.heatmap_selected_residues = []

                st.checkbox(
                    f"Cluster {_cid}  ({_n} pockets, avg prob: {_avg:.3f})",
                    key=f"pipe_cluster_cb_{_cid}",
                    on_change=_on_change,
                )
                st.markdown(f'<div style="height:{_gap:.0f}px"></div>', unsafe_allow_html=True)

        with heat_col:
            st.plotly_chart(fig_heat, use_container_width=True, key="pipe_consensus_heatmap")
            st.caption(
                "Each row = a cluster. Each column = a residue. "
                "Color = how consistently the residue appears (0 = never, 1 = always)."
            )

        with viewer_col:
            sel_id = st.session_state.heatmap_selected_cluster_id
            if sel_id is not None:
                sel_path = st.session_state.heatmap_selected_pdb_path
                sel_residues = st.session_state.heatmap_selected_residues
                rep = cluster_to_rep.get(sel_id)
                st.markdown(f"**Cluster {sel_id}** — Representative Structure")
                if rep is not None:
                    m1, m2 = st.columns(2)
                    m1.metric("Probability", f"{rep.get('probability', 0):.3f}")
                    m2.metric("Residues", len(sel_residues))
                if sel_path and os.path.exists(sel_path):
                    _show_molecule_3d_with_pocket(sel_path, sel_residues)
                else:
                    st.warning(f"PDB not found: `{sel_path}`")
            else:
                st.info("← Check a cluster to view its 3D structure here")


    except Exception as e:
        st.error(f"Error loading clustering results: {e}")
        logger.error(f"Pipeline inline cluster results error: {e}", exc_info=True)


# Session state
initialize_session_state()

if 'pipeline_job_id' not in st.session_state:
    st.session_state.pipeline_job_id = None
if 'pipeline_task_id' not in st.session_state:
    st.session_state.pipeline_task_id = None
if 'pipeline_status' not in st.session_state:
    st.session_state.pipeline_status = 'idle'


# Real progress boundaries matching _run_stage calls in tasks.py:
#   0-25   extract_to_pdb
#   25-60  detect_pockets
#   60-100 cluster_pockets
_STAGES = [
    ('Extract Frames',    0,  25, 'file-earmark-arrow-down'),
    ('Detect Pockets',   25,  60, 'search'),
    ('Cluster Pockets',  60, 100, 'diagram-3'),
]


def _stage_class(progress, start, end):
    if progress >= end:
        return 'stage-done'
    elif progress >= start:
        return 'stage-active'
    return 'stage-pending'


def show_stage_indicators(progress, current_step=''):
    boxes = "".join(
        f'<div class="stage-box {_stage_class(progress, s, e)}">{name}</div>'
        for name, s, e, _ in _STAGES
    )
    st.markdown(f'<div class="stage-indicator">{boxes}</div>', unsafe_allow_html=True)
    if current_step:
        st.caption(f"▶ {current_step}")


# ── Status banner if a task is running ─────────────────────────────────
if st.session_state.pipeline_task_id:
    try:
        _task = celery_app.AsyncResult(st.session_state.pipeline_task_id)
        if _task.state == 'PENDING':
            show_stage_indicators(0, 'Waiting in queue…')
            st.progress(0, text="Pending…")
            st.info("⏳ Pipeline task is pending in the Celery queue.")
            time.sleep(4)
            st.rerun()
        elif _task.state == 'PROGRESS':
            _info = _task.info or {}
            _prog = _info.get('progress', 0)
            _step = _info.get('current_step', 'Processing…')
            _stage = _info.get('stage', '')

            show_stage_indicators(_prog, _step)
            st.progress(_prog / 100, text=f"{_prog}%")

            # Contextual metrics depending on stage
            if _stage in ('detect', 'cluster', 'cluster_done'):
                _frames = _info.get('frames_extracted')
                if _frames is not None:
                    st.caption(f"📁 {_frames} frames extracted")
            if _stage in ('cluster', 'cluster_done'):
                _pockets = _info.get('pockets_detected')
                if _pockets is not None:
                    st.caption(f"🔍 {_pockets} pockets detected")

            time.sleep(3)
            st.rerun()
        elif _task.state == 'SUCCESS':
            result = _task.result or {}
            show_stage_indicators(100, 'All stages complete')
            st.success("✅ Full pipeline completed successfully!")
            col1, col2, col3 = st.columns(3)
            col1.metric("Frames extracted", result.get('frames_extracted', '—'))
            col2.metric("Pockets detected", result.get('pockets_detected', '—'))
            col3.metric("Cluster representatives", result.get('representatives', '—'))

            cluster_job = result.get('cluster_job_id', st.session_state.pipeline_job_id)
            if cluster_job:
                _show_pipeline_cluster_inline(cluster_job)
        elif _task.state == 'FAILURE':
            show_stage_indicators(0, 'Pipeline failed')
            st.error("❌ Pipeline failed.")
            _info = _task.info or {}
            st.error(f"Error: {_info.get('exc_message', str(_task.info))}")
    except Exception as e:
        st.warning(f"Could not retrieve task status: {e}")

    if st.button("🔄 Reset / Start New Pipeline"):
        st.session_state.pipeline_task_id = None
        st.session_state.pipeline_job_id = None
        st.session_state.pipeline_status = 'idle'
        st.session_state.heatmap_selected_cluster_id = None
        st.rerun()

    st.stop()


# ── Input form ──────────────────────────────────────────────────────────
st.markdown("### 📁 Input Files")

with st.form("pipeline_form"):
    col_traj, col_topo = st.columns(2)

    with col_traj:
        traj_file = st.file_uploader(
            "Trajectory (.xtc)",
            type=['xtc'],
            help="GROMACS XTC trajectory file"
        )
    with col_topo:
        topo_file = st.file_uploader(
            "Topology (.pdb or .gro)",
            type=['pdb', 'gro'],
            help="Topology/structure file matching the trajectory"
        )

    st.markdown("### ⚙️ Processing Parameters")

    col_stride, col_threads = st.columns(2)
    with col_stride:
        stride = st.slider("Frame Stride", min_value=1, max_value=100, value=10,
                           help="Extract every Nth frame from trajectory")
    with col_threads:
        num_threads = st.slider("CPU Threads", min_value=1, max_value=16, value=4,
                                help="Number of parallel threads for pocket detection")

    col_prob, col_method = st.columns(2)
    with col_prob:
        min_prob = st.slider("Min Pocket Probability", min_value=0.0, max_value=1.0, value=0.5, step=0.05,
                             help="Minimum p2rank probability threshold")
    with col_method:
        clustering_method = st.selectbox("Clustering Method", options=['dbscan', 'kmeans', 'hierarchical'],
                                         help="Algorithm for grouping similar pockets")

    submitted = st.form_submit_button("🚀 Launch Full Pipeline", type="primary", use_container_width=True)


if submitted:
    if not traj_file:
        st.error("❌ Please upload a trajectory (.xtc) file.")
        st.stop()
    if not topo_file:
        st.error("❌ Please upload a topology (.pdb or .gro) file.")
        st.stop()

    # Rate limit check
    try:
        check_task_rate_limit()
    except RateLimitExceeded as e:
        st.error(f"⏳ Task rate limit exceeded: {e}")
        st.stop()

    job_id = f"pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}"
    job_upload_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_upload_dir, exist_ok=True)

    # Save trajectory
    traj_path = os.path.join(job_upload_dir, traj_file.name)
    with open(traj_path, 'wb') as f:
        f.write(traj_file.getbuffer())

    # Save topology
    topo_path = os.path.join(job_upload_dir, topo_file.name)
    with open(topo_path, 'wb') as f:
        f.write(topo_file.getbuffer())

    # Submit Celery task
    task = run_pockethunter_pipeline.delay(
        xtc_file_path=traj_path,
        topology_file_path=topo_path,
        job_id=job_id,
        stride=stride,
        num_threads=num_threads,
        min_prob=min_prob,
        clustering_method=clustering_method,
    )

    st.session_state.pipeline_job_id = job_id
    st.session_state.pipeline_task_id = task.id
    st.session_state.pipeline_status = 'running'
    st.session_state.cached_job_ids['pipeline'] = job_id

    st.success(f"✅ Pipeline started! Job ID: `{job_id}`")
    time.sleep(1)
    st.rerun()
