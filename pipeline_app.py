import streamlit as st
import os
import zipfile
import uuid
import json
from datetime import datetime
import time
from pathlib import Path
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
        Extract → Detect → Cluster → (optional) Dock — all in one shot
    </p>
</div>
""", unsafe_allow_html=True)

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
#   60-80  cluster_pockets
#   80-97  docking (optional)
_STAGES = [
    ('Extract Frames',    0,  25, 'file-earmark-arrow-down'),
    ('Detect Pockets',   25,  60, 'search'),
    ('Cluster Pockets',  60,  80, 'diagram-3'),
    ('Dock (optional)',  80, 100, 'flask'),
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
            if _stage in ('detect', 'cluster', 'cluster_done', 'docking'):
                _frames = _info.get('frames_extracted')
                if _frames is not None:
                    st.caption(f"📁 {_frames} frames extracted")
            if _stage in ('cluster', 'cluster_done', 'docking'):
                _pockets = _info.get('pockets_detected')
                if _pockets is not None:
                    st.caption(f"🔍 {_pockets} pockets detected")
            if _stage == 'docking':
                _pairs_done = _info.get('pairs_done', 0)
                _pairs_total = _info.get('pairs_total', 0)
                if _pairs_total > 0:
                    st.progress(_pairs_done / _pairs_total,
                                text=f"Docking: {_pairs_done}/{_pairs_total} pairs")

            time.sleep(3)
            st.rerun()
        elif _task.state == 'SUCCESS':
            result = _task.result or {}
            show_stage_indicators(100, 'All stages complete')
            st.success("✅ Full pipeline completed successfully!")
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Frames extracted", result.get('frames_extracted', '—'))
            col2.metric("Pockets detected", result.get('pockets_detected', '—'))
            col3.metric("Cluster representatives", result.get('representatives', '—'))
            col4.metric("Docking poses", result.get('docking_poses', '—') or '—')
            if result.get('docking_error'):
                st.warning(f"⚠️ Docking encountered an error: {result['docking_error']}")

            cluster_job = result.get('cluster_job_id', st.session_state.pipeline_job_id)
            if cluster_job and st.button("📊 View Clustering Results", type="primary", use_container_width=True):
                st.session_state.cached_job_ids['cluster'] = cluster_job
                st.session_state.cluster_job_id = cluster_job
                st.session_state.pending_nav = "Step 3: Cluster Pockets"
                st.rerun()
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

    st.markdown("### 🔬 Optional: Molecular Docking")
    include_docking = st.checkbox("Run docking after clustering", value=False)
    if include_docking:
        st.info("ℹ️ Docking will run automatically on **all** cluster representatives. To hand-pick specific clusters first, leave this unchecked — then use the heatmap in Step 3 to select clusters before docking.")

    ligand_files_uploaded = None
    num_poses = 10
    exhaustiveness = 8
    ph_value = 7.4
    box_size = 20.0

    if include_docking:
        st.info(f"Ligand limit: {Config.MAX_DOCKING_LIGANDS} files. Exhaustiveness cap: {Config.MAX_DOCKING_EXHAUSTIVENESS}.")
        ligand_files_uploaded = st.file_uploader(
            "Ligand Files (PDBQT or ZIP)",
            type=['pdbqt', 'zip'],
            accept_multiple_files=True,
            help="Upload PDBQT ligands or a ZIP archive of PDBQT files"
        )
        dock_col1, dock_col2, dock_col3, dock_col4 = st.columns(4)
        with dock_col1:
            num_poses = st.slider("Poses per Ligand", 1, 20, 10)
        with dock_col2:
            exhaustiveness = st.slider("Exhaustiveness", 1, Config.MAX_DOCKING_EXHAUSTIVENESS,
                                       min(8, Config.MAX_DOCKING_EXHAUSTIVENESS))
        with dock_col3:
            ph_value = st.slider("pH", 4.0, 10.0, 7.4, step=0.1)
        with dock_col4:
            box_size = st.slider("Box Size (Å)", 10.0, 50.0, 20.0, step=1.0)

    submitted = st.form_submit_button("🚀 Launch Full Pipeline", type="primary", use_container_width=True)


if submitted:
    if not traj_file:
        st.error("❌ Please upload a trajectory (.xtc) file.")
        st.stop()
    if not topo_file:
        st.error("❌ Please upload a topology (.pdb or .gro) file.")
        st.stop()
    if include_docking and not ligand_files_uploaded:
        st.error("❌ Please upload at least one ligand file to enable docking.")
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

    # Save ligands if provided
    ligand_folder = None
    if include_docking and ligand_files_uploaded:
        ligand_dir = os.path.join(job_upload_dir, 'ligands')
        os.makedirs(ligand_dir, exist_ok=True)
        collected = []
        for lf in ligand_files_uploaded:
            lf_path = os.path.join(ligand_dir, lf.name)
            with open(lf_path, 'wb') as f:
                f.write(lf.getbuffer())
            if lf.name.endswith('.zip'):
                with zipfile.ZipFile(lf_path, 'r') as zf:
                    zf.extractall(ligand_dir)
                    for fname in zf.namelist():
                        if fname.endswith('.pdbqt'):
                            collected.append(os.path.join(ligand_dir, fname))
            elif lf.name.endswith('.pdbqt'):
                collected.append(lf_path)
        if len(collected) > Config.MAX_DOCKING_LIGANDS:
            st.warning(f"⚠️ Ligand count capped from {len(collected)} to {Config.MAX_DOCKING_LIGANDS}.")
            for excess in collected[Config.MAX_DOCKING_LIGANDS:]:
                try:
                    os.remove(excess)
                except OSError:
                    pass
            collected = collected[:Config.MAX_DOCKING_LIGANDS]
        ligand_folder = ligand_dir

    # Submit Celery task
    task = run_pockethunter_pipeline.delay(
        xtc_file_path=traj_path,
        topology_file_path=topo_path,
        job_id=job_id,
        stride=stride,
        num_threads=num_threads,
        min_prob=min_prob,
        clustering_method=clustering_method,
        run_docking=include_docking,
        ligand_folder=ligand_folder,
        num_poses=num_poses,
        exhaustiveness=exhaustiveness,
        ph_value=ph_value,
        box_size_x=box_size,
        box_size_y=box_size,
        box_size_z=box_size,
    )

    st.session_state.pipeline_job_id = job_id
    st.session_state.pipeline_task_id = task.id
    st.session_state.pipeline_status = 'running'
    st.session_state.cached_job_ids['pipeline'] = job_id

    st.success(f"✅ Pipeline started! Job ID: `{job_id}`")
    time.sleep(1)
    st.rerun()
