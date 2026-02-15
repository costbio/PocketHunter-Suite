import streamlit as st
import os
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import time
import json
import uuid
import zipfile
from tasks import run_detect_pockets_task
from celery_app import celery_app
from config import Config
from security import handle_file_upload_secure, SecurityError, FileValidator
from rate_limiter import RateLimitExceeded, check_task_rate_limit
from logging_config import setup_logging
from pathlib import Path

# Use Config for directories
UPLOAD_DIR = str(Config.UPLOAD_DIR)
RESULTS_DIR = str(Config.RESULTS_DIR)

# Setup logging
logger = setup_logging(__name__)

# Custom CSS
st.markdown("""
<style>
    .detect-header {
        background: linear-gradient(135deg, #66BB6A 0%, #43A047 50%, #2E7D32 100%);
        padding: 2rem;
        border-radius: 20px;
        margin-bottom: 2rem;
        color: white;
        text-align: center;
        box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        border: 1px solid rgba(255, 255, 255, 0.18);
    }

    .detect-header h1 {
        margin: 0;
        font-size: 2.5rem;
        font-weight: 700;
        text-shadow: 2px 2px 4px rgba(0,0,0,0.1);
    }

    .detect-card {
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
        background: linear-gradient(135deg, #E8F5E9 0%, #C8E6C9 100%);
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
        color: #2E7D32;
        margin: 0.5rem 0;
    }

    .metric-label {
        font-size: 0.9rem;
        color: #666;
        text-transform: uppercase;
        letter-spacing: 1px;
    }

    .job-id-display {
        background: linear-gradient(135deg, #66BB6A 0%, #43A047 100%);
        color: white;
        padding: 1.2rem;
        border-radius: 15px;
        font-family: 'Courier New', monospace;
        font-size: 1.1rem;
        text-align: center;
        margin: 1.5rem 0;
        box-shadow: 0 4px 20px rgba(102, 187, 106, 0.3);
        border: 1px solid rgba(255, 255, 255, 0.2);
    }
</style>
""", unsafe_allow_html=True)

# Header
st.markdown("""
<div class="detect-header">
    <h1>🔍 Step 2: Pocket Detection</h1>
    <p style="font-size: 1.2rem; margin-top: 0.5rem;">Identify ligand-binding pockets in protein structures</p>
</div>
""", unsafe_allow_html=True)

# Session state initialization
if 'detect_job_id' not in st.session_state:
    st.session_state.detect_job_id = None
if 'detect_task_id' not in st.session_state:
    st.session_state.detect_task_id = None
if 'detect_status' not in st.session_state:
    st.session_state.detect_status = 'idle'
if 'cached_job_ids' not in st.session_state:
    st.session_state.cached_job_ids = {}

# Helper functions
def extract_zip_to_directory(zip_path, extract_dir):
    """Extract ZIP file to directory and return list of PDB files"""
    try:
        FileValidator.validate_zip_file(Path(zip_path))
        logger.info(f"ZIP file validated: {zip_path}")
    except SecurityError as e:
        logger.error(f"ZIP validation failed: {e}")
        raise

    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(extract_dir)

    pdb_files = []
    for root, dirs, files in os.walk(extract_dir):
        for file in files:
            if file.endswith('.pdb'):
                pdb_files.append(os.path.join(root, file))
    return pdb_files

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

# ── Status Banner ──────────────────────────────────────────────────────
if st.session_state.detect_task_id:
    try:
        _task = celery_app.AsyncResult(st.session_state.detect_task_id)
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
            st.success(f"✅ Detection completed! Pockets detected: {_result.get('pockets_detected', 'N/A')} | Time: {_result.get('processing_time', 0):.1f}s")
            st.progress(1.0)
            st.session_state.detect_status = 'completed'
            st.session_state.cached_job_ids['detect'] = st.session_state.detect_job_id
        elif _task.state == 'FAILURE':
            st.error(f"❌ Detection failed: {_task.info}")
            st.session_state.detect_status = 'failed'
    except Exception as e:
        logger.error(f"Status banner error: {e}")

# ── Input Configuration ───────────────────────────────────────────────
st.markdown("### 📁 Input Configuration")

if st.session_state.detect_job_id:
    st.markdown(f"""
    <div class="job-id-display">
        🔑 Current Job ID: {st.session_state.detect_job_id}
    </div>
    """, unsafe_allow_html=True)

input_col1, input_col2 = st.columns(2)

with input_col1:
    st.markdown("**Option 1: Use Previous Step**")
    cached_extract_id = st.session_state.cached_job_ids.get('extract', '')
    extract_job_id = st.text_input(
        "Job ID from Step 1:",
        value=cached_extract_id,
        key="detect_extract_job_id",
        help="Enter the Job ID from frame extraction"
    )

with input_col2:
    st.markdown("**Option 2: Upload PDB ZIP**")
    pdb_zip = st.file_uploader(
        "Upload PDB Files:",
        type=['zip'],
        key="detect_pdb_zip",
        help="Upload ZIP containing PDB files"
    )

st.markdown("#### ⚙️ Detection Parameters")

param_col1, param_col2 = st.columns(2)

with param_col1:
    num_threads = st.number_input(
        "Number of Threads",
        min_value=1,
        max_value=16,
        value=4,
        key="detect_threads",
        help="CPU threads for pocket detection"
    )

with param_col2:
    st.markdown("<br>", unsafe_allow_html=True)
    st.info("💡 Using P2Rank for pocket prediction")

# Run button
st.markdown("---")
if st.button("🚀 Start Pocket Detection", type="primary", use_container_width=True):
    input_pdb_path = None
    input_source = None

    if extract_job_id and extract_job_id.strip():
        extract_output_dir = os.path.join(RESULTS_DIR, extract_job_id.strip(), "pdbs")
        if os.path.exists(extract_output_dir) and os.listdir(extract_output_dir):
            input_pdb_path = extract_output_dir
            input_source = f"Step 1 results (Job ID: {extract_job_id.strip()})"
        else:
            st.error(f"PDB files not found for Job ID: {extract_job_id.strip()}")
            st.stop()

    elif pdb_zip:
        job_id = f"detect_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        extract_dir = os.path.join(UPLOAD_DIR, job_id, "extracted_pdbs")
        os.makedirs(extract_dir, exist_ok=True)
        try:
            zip_path = handle_file_upload_secure(pdb_zip, job_id, "pdbs_")
            logger.info(f"ZIP file uploaded for job {job_id}")
        except SecurityError as e:
            st.error(f"❌ File upload failed: {e}")
            logger.error(f"Security error during ZIP upload: {e}")
            st.stop()
        if zip_path:
            pdb_files = extract_zip_to_directory(zip_path, extract_dir)
            if pdb_files:
                input_pdb_path = extract_dir
                input_source = f"Uploaded ZIP ({len(pdb_files)} PDB files)"
            else:
                st.error("No PDB files found in ZIP")
                st.stop()
    else:
        st.error("Please provide input using one of the options above.")
        st.stop()

    if input_pdb_path:
        job_id = f"detect_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        st.session_state.detect_job_id = job_id

        try:
            check_task_rate_limit()
        except RateLimitExceeded as e:
            st.error(f"⏳ Task submission rate limit exceeded: {e}")
            st.info(f"Please wait {e.retry_after:.0f} seconds before submitting another task.")
            logger.warning(f"Task rate limit exceeded for job {job_id}: {e}")
            st.stop()

        update_job_status(job_id, 'submitted', 'Initializing pocket detection')
        st.session_state.detect_status = 'running'

        with st.spinner("Starting pocket detection..."):
            task = run_detect_pockets_task.delay(
                input_pdb_path_abs=os.path.abspath(input_pdb_path),
                job_id=job_id,
                numthreads=num_threads
            )
            st.session_state.detect_task_id = task.id
            update_job_status(job_id, 'running', 'Pocket detection started', task_id=task.id)

        st.success(f"✅ Detection started! Job ID: `{job_id}`")
        st.info(f"📂 Input: {input_source}")

# ── Results ────────────────────────────────────────────────────────────
# Determine which job to show results for
results_job_id = st.session_state.detect_job_id

# Allow loading previous results
with st.expander("📂 Load previous results"):
    load_job_id = st.text_input(
        "Enter Detection Job ID:",
        value="",
        placeholder="e.g., detect_20250815_143022_a1b2c3d4",
        key="detect_load_job_id",
        help="Enter a detection job ID to view its results"
    )
    if st.button("🔍 Load Results"):
        if load_job_id:
            st.session_state.detect_job_id = load_job_id
            results_job_id = load_job_id
            st.rerun()

if results_job_id:
    pockets_output_dir = os.path.join(RESULTS_DIR, results_job_id, "pockets")
    pockets_csv_file = os.path.join(pockets_output_dir, "pockets.csv")

    if os.path.exists(pockets_csv_file):
        try:
            df_pockets = pd.read_csv(pockets_csv_file)

            # Compute numeric residue count from residue name strings
            if 'residues' in df_pockets.columns and df_pockets['residues'].dtype == object:
                df_pockets['num_residues'] = df_pockets['residues'].apply(
                    lambda x: len(str(x).split()) if pd.notna(x) else 0
                )
            elif 'residues' in df_pockets.columns:
                df_pockets['num_residues'] = df_pockets['residues']
            else:
                df_pockets['num_residues'] = 0

            if len(df_pockets) == 0:
                st.warning("⚠️ Detection completed but no pockets were found in the input structures.")
            else:
                st.markdown("---")
                st.markdown("### 🎯 Detection Results")

                # Overview metrics
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.markdown(f"""
                    <div class="metric-card">
                        <div class="metric-label">Total Pockets</div>
                        <div class="metric-value">{len(df_pockets)}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col2:
                    st.markdown(f"""
                    <div class="metric-card">
                        <div class="metric-label">Avg Probability</div>
                        <div class="metric-value">{df_pockets['probability'].mean():.3f}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col3:
                    high_conf = len(df_pockets[df_pockets['probability'] >= 0.7])
                    st.markdown(f"""
                    <div class="metric-card">
                        <div class="metric-label">High Confidence</div>
                        <div class="metric-value">{high_conf}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col4:
                    st.markdown(f"""
                    <div class="metric-card">
                        <div class="metric-label">Best Probability</div>
                        <div class="metric-value">{df_pockets['probability'].max():.3f}</div>
                    </div>
                    """, unsafe_allow_html=True)

                # Sub-tabs for results
                results_tab1, results_tab2, results_tab3 = st.tabs([
                    "📋 Pocket Table",
                    "📈 Distribution Analysis",
                    "💾 Download"
                ])

                with results_tab1:
                    df_display = df_pockets.sort_values('probability', ascending=False)

                    def get_confidence_badge(prob):
                        if prob >= 0.7:
                            return "🟢 High"
                        elif prob >= 0.4:
                            return "🟡 Medium"
                        else:
                            return "🔴 Low"

                    df_display['Confidence'] = df_display['probability'].apply(get_confidence_badge)

                    col1, col2 = st.columns(2)
                    with col1:
                        min_prob_filter = st.slider(
                            "Minimum Probability:",
                            0.0, 1.0, 0.0, 0.05,
                            help="Filter pockets by minimum probability"
                        )
                    with col2:
                        confidence_filter = st.multiselect(
                            "Filter by Confidence:",
                            options=['🟢 High', '🟡 Medium', '🔴 Low'],
                            default=['🟢 High', '🟡 Medium', '🔴 Low']
                        )

                    df_filtered = df_display[df_display['probability'] >= min_prob_filter]
                    if confidence_filter:
                        df_filtered = df_filtered[df_filtered['Confidence'].isin(confidence_filter)]

                    st.dataframe(
                        df_filtered[['File name', 'pocket_index', 'probability', 'num_residues', 'Confidence']],
                        use_container_width=True,
                        height=400
                    )
                    st.info(f"📊 Showing {len(df_filtered)} of {len(df_pockets)} pockets")

                with results_tab2:
                    col1, col2 = st.columns(2)
                    with col1:
                        fig_hist = px.histogram(
                            df_pockets, x='probability',
                            title='Probability Distribution',
                            nbins=30, color_discrete_sequence=['#66BB6A']
                        )
                        fig_hist.update_layout(xaxis_title="Binding Probability", yaxis_title="Number of Pockets", showlegend=False)
                        st.plotly_chart(fig_hist, use_container_width=True)
                    with col2:
                        fig_residues = px.box(
                            df_pockets, y='num_residues',
                            title='Residue Count Distribution',
                            color_discrete_sequence=['#43A047']
                        )
                        fig_residues.update_layout(yaxis_title="Number of Residues", showlegend=False)
                        st.plotly_chart(fig_residues, use_container_width=True)

                    fig_scatter = px.scatter(
                        df_pockets, x='num_residues', y='probability',
                        size='probability', color='probability',
                        title='Pocket Probability vs Size',
                        labels={'num_residues': 'Number of Residues', 'probability': 'Binding Probability'},
                        color_continuous_scale='Greens',
                        hover_data=['File name', 'pocket_index']
                    )
                    fig_scatter.update_traces(marker=dict(line=dict(width=1, color='DarkGreen')))
                    st.plotly_chart(fig_scatter, use_container_width=True)

                    stats_col1, stats_col2, stats_col3, stats_col4 = st.columns(4)
                    with stats_col1:
                        st.metric("Mean Probability", f"{df_pockets['probability'].mean():.3f}")
                    with stats_col2:
                        st.metric("Median Probability", f"{df_pockets['probability'].median():.3f}")
                    with stats_col3:
                        st.metric("Std Dev", f"{df_pockets['probability'].std():.3f}")
                    with stats_col4:
                        st.metric("High Confidence (>=0.7)", len(df_pockets[df_pockets['probability'] >= 0.7]))

                with results_tab3:
                    col1, col2 = st.columns(2)
                    with col1:
                        st.markdown("**📄 Data Files**")
                        csv_data = df_pockets.to_csv(index=False)
                        st.download_button(
                            label="📥 Download All Pockets (CSV)",
                            data=csv_data,
                            file_name=f"pockets_{results_job_id}.csv",
                            mime="text/csv",
                            use_container_width=True
                        )
                        df_high_conf = df_pockets[df_pockets['probability'] >= 0.7]
                        if len(df_high_conf) > 0:
                            hc_csv = df_high_conf.to_csv(index=False)
                            st.download_button(
                                label="📥 Download High Confidence (CSV)",
                                data=hc_csv,
                                file_name=f"high_confidence_pockets_{results_job_id}.csv",
                                mime="text/csv",
                                use_container_width=True
                            )
                    with col2:
                        st.markdown("**📦 Structure Files**")
                        if st.button("🔄 Generate PDB Archive", use_container_width=True):
                            with st.spinner("Creating archive..."):
                                pdbs_dir = os.path.join(RESULTS_DIR, results_job_id, "pdbs")
                                zip_path = os.path.join(pockets_output_dir, 'pockets_pdbs.zip')
                                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                                    for pdb_file in Path(pdbs_dir).glob('*.pdb'):
                                        zipf.write(pdb_file, pdb_file.name)
                                st.success("✅ Archive created!")
                        zip_path = os.path.join(pockets_output_dir, 'pockets_pdbs.zip')
                        if os.path.exists(zip_path):
                            with open(zip_path, 'rb') as f:
                                st.download_button(
                                    label="📥 Download All PDB Files (ZIP)",
                                    data=f.read(),
                                    file_name=f"pockets_pdbs_{results_job_id}.zip",
                                    mime="application/zip",
                                    use_container_width=True
                                )

                st.markdown("---")
                st.info("💡 Use this Job ID in Step 3: Cluster Pockets to group similar pockets")

        except Exception as e:
            st.error(f"Error loading results: {e}")
            logger.error(f"Results loading error: {e}", exc_info=True)

# Auto-refresh when task is running
if st.session_state.detect_status == 'running' and st.session_state.detect_task_id:
    try:
        task = celery_app.AsyncResult(st.session_state.detect_task_id)
        if task.ready():
            st.session_state.detect_status = 'completed'
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
    <p>🔍 Pocket Detection powered by P2Rank | Part of the PocketHunter Suite</p>
    <p style='font-size: 0.85rem; margin-top: 0.5rem;'>
        💡 Tip: High-confidence pockets (≥0.7) are recommended for further analysis
    </p>
</div>
""", unsafe_allow_html=True)
