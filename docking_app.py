import streamlit as st
import os
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import time
import json
import zipfile
import shutil
import uuid
import math
import glob
import subprocess
from pathlib import Path
from tasks import run_docking_task
from celery_app import celery_app
from config import Config
from security import FileValidator, SecurityError
from rate_limiter import RateLimitExceeded, check_task_rate_limit, check_upload_rate_limit
from logging_config import setup_logging
from session_state import initialize_session_state, get_pdb_selection_key, render_load_previous_widget
from cluster_labels import describe_cluster_spatially
import py3Dmol
import streamlit.components.v1 as components

# Use Config for directories
RESULTS_DIR = str(Config.RESULTS_DIR)
UPLOAD_DIR = str(Config.UPLOAD_DIR)

# Setup logging
logger = setup_logging(__name__)


def update_job_status(job_id, status, step=None, task_id=None, result_info=None):
    """Update job status file"""
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
    from datetime import datetime
    current_status['last_updated'] = datetime.now().isoformat()

    with open(status_file, 'w') as f:
        json.dump(current_status, f, indent=4)

# Page configuration is handled by main.py

# Brutalist header — matches main.py .bh* pattern, no per-page CSS needed.
st.markdown("""
<div class="bh" style="margin-top: 4px;">
    <div class="bh-row">
        <span class="bh-title">Step 3 / Molecular Docking</span>
        <span class="bh-version">[CLUSTERS → DOCK]</span>
    </div>
    <div class="bh-rule"></div>
    <div class="bh-stages">
        Ligand–protein docking with 3D visualization
    </div>
</div>
""", unsafe_allow_html=True)

# Initialize session state using centralized module
initialize_session_state()

# Additional docking-specific state (backwards compatibility)
if 'docking_job_id' not in st.session_state:
    st.session_state.docking_job_id = None
if 'docking_task_id' not in st.session_state:
    st.session_state.docking_task_id = None
if 'docking_selected_pdbs' not in st.session_state:
    st.session_state.docking_selected_pdbs = {}

# Cache the auto-suggested box size (computed once per cluster job).
if 'docking_auto_box' not in st.session_state:
    st.session_state.docking_auto_box = None
if 'docking_auto_box_cluster' not in st.session_state:
    st.session_state.docking_auto_box_cluster = None

# Refresh the auto-suggested box size from the currently-known cluster job, so
# the sidebar's auto-size button (which renders BEFORE the main content) has
# the right values to offer.
_known_cluster = (
    st.session_state.get('docking_cluster_job_id', '')
    or st.session_state.cached_job_ids.get('cluster')
    or ''
)
_known_extract = st.session_state.get('docking_extract_job_id', '') or None


def _compute_auto_box_for_job(cluster_job_id: str, extract_job_id: str | None = None):
    """Compute box dimensions from the highest-probability cluster representative.

    Thin wrapper around ``docking_selection.auto_box_for_selection`` that adds
    session-state caching keyed by ``cluster_job_id`` (the helper itself is
    pure / cacheless). Returns ``(sx, sy, sz, cluster_label)`` or ``None``.
    """
    if not cluster_job_id:
        return None
    from security import FileValidator, SecurityError
    try:
        cluster_job_id = FileValidator.validate_job_id(cluster_job_id)
    except SecurityError:
        return None
    if (st.session_state.docking_auto_box_cluster == cluster_job_id
            and st.session_state.docking_auto_box is not None):
        return st.session_state.docking_auto_box

    reps_csv = os.path.join(RESULTS_DIR, cluster_job_id, "pocket_clusters",
                            "cluster_representatives.csv")
    if not os.path.exists(reps_csv):
        return None
    try:
        df = pd.read_csv(reps_csv)
    except Exception as e:  # noqa: BLE001 — auto-size is best-effort
        logger.warning(f"Auto-box CSV load failed for {cluster_job_id}: {e}")
        return None

    from docking_selection import auto_box_for_selection
    result = auto_box_for_selection(
        df, cluster_job_id,
        selected_clusters=None,
        padding=4.0,
        extra_pdb_source_job_id=extract_job_id,
    )
    if result is not None:
        st.session_state.docking_auto_box = result
        st.session_state.docking_auto_box_cluster = cluster_job_id
    return result


# Compute (or refresh) the auto-suggested box size for whatever cluster
# the user has previously loaded. Best-effort; if it fails the sidebar
# falls back to the static 20Å default.
if _known_cluster:
    _compute_auto_box_for_job(_known_cluster, _known_extract)


# Sidebar for configuration
with st.sidebar:
    st.markdown("### ⚙️ Docking Configuration")

    # SMINA executable path
    smina_path = st.text_input(
        "SMINA Executable Path",
        value="smina",
        help="Path to SMINA executable. Leave as 'smina' if it's in your PATH."
    )

    # Docking parameters
    st.markdown("#### 🎯 Docking Parameters")

    num_poses = st.slider(
        "Number of Poses",
        min_value=1,
        max_value=50,
        value=10,
        help="Maximum number of docking poses to generate per ligand"
    )

    exhaustiveness = st.slider(
        "Exhaustiveness",
        min_value=1,
        max_value=Config.MAX_DOCKING_EXHAUSTIVENESS,
        value=min(8, Config.MAX_DOCKING_EXHAUSTIVENESS),
        help=f"Accuracy of docking calculations (higher = more accurate but slower, max={Config.MAX_DOCKING_EXHAUSTIVENESS})"
    )

    # pH for protonation
    ph_value = st.slider(
        "pH for Protonation",
        min_value=4.0,
        max_value=10.0,
        value=7.4,
        step=0.1,
        help="pH value for ligand and protein protonation"
    )

    # Box size parameters
    st.markdown("#### 📦 Binding Site Box")

    auto_box = st.session_state.get('docking_auto_box')
    if auto_box:
        sx_auto, sy_auto, sz_auto, cluster_label = auto_box
        if st.button(
            f"🎯 Auto-size from {cluster_label}",
            use_container_width=True,
            help=(
                f"Suggested: X={sx_auto}, Y={sy_auto}, Z={sz_auto} Å "
                "(highest-probability representative + 4 Å padding on each side). "
                "Values are clamped to the 10–50 Å slider range."
            ),
        ):
            st.session_state['box_x'] = float(min(max(sx_auto, 10.0), 50.0))
            st.session_state['box_y'] = float(min(max(sy_auto, 10.0), 50.0))
            st.session_state['box_z'] = float(min(max(sz_auto, 10.0), 50.0))
            st.rerun()
    else:
        st.caption("💡 Load a cluster job below to enable auto-size from the top representative.")

    box_size_x = st.slider(
        "Box Size X (Å)",
        min_value=10.0,
        max_value=50.0,
        value=20.0,
        step=1.0,
        key="box_x",
        help="Size of docking box in X direction"
    )

    box_size_y = st.slider(
        "Box Size Y (Å)",
        min_value=10.0,
        max_value=50.0,
        value=20.0,
        step=1.0,
        key="box_y",
        help="Size of docking box in Y direction"
    )

    box_size_z = st.slider(
        "Box Size Z (Å)",
        min_value=10.0,
        max_value=50.0,
        value=20.0,
        step=1.0,
        key="box_z",
        help="Size of docking box in Z direction"
    )

    st.markdown("---")
    st.markdown("### 🎨 Visualization Settings")

    color_scheme = st.selectbox(
        "Color Scheme",
        ["spectrum", "chain", "secondary", "residue"],
        help="Color scheme for 3D visualization"
    )

    surface_opacity = st.slider(
        "Surface Opacity",
        min_value=0.0,
        max_value=1.0,
        value=0.7,
        step=0.1,
        help="Opacity of molecular surface"
    )


# 3D viewer + affinity classifier + multi-model SDF extractor all live in
# docking_visualization. Local names preserved for downstream call sites.
from docking_visualization import (
    classify_affinity,
    extract_sdf_model,
    get_binding_site_residues as _get_binding_site_residues,
    show_molecule_3d,
)


# Main content area - Create tabs for different views
tab_setup, tab_results = st.tabs(["🎯 Setup & Launch", "📊 Results & 3D Viewer"])

with tab_setup:
    st.markdown("### 🎯 Job Configuration")

    # Generate job ID once and store in session state
    if not st.session_state.get('docking_display_job_id'):
        st.session_state.docking_display_job_id = f"docking_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}"
    job_id = st.session_state.docking_display_job_id

    # Display job ID with reset button
    col_id, col_reset = st.columns([4, 1])
    with col_id:
        st.caption(f"Current docking job: `{job_id}`")
    with col_reset:
        if st.button("🔄 New Job", help="Generate a new job ID for a fresh docking configuration"):
            st.session_state.docking_display_job_id = f"docking_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}"
            st.rerun()

    st.info("💡 **Save this Job ID** - you can use it to monitor progress in the Task Monitor page!")

    # Cluster selection
    st.markdown("### 📁 Select Cluster Results")

    # Handle heatmap pre-selection from Step 2 (Cluster)
    heatmap_preselect = st.session_state.pop('heatmap_preselected_for_docking', None)
    if heatmap_preselect:
        st.info(f"Clusters {heatmap_preselect['cluster_ids']} from job `{heatmap_preselect['cluster_job_id']}` pre-selected from heatmap.")
        default_cluster_job = heatmap_preselect['cluster_job_id']
        preselected_ids = set(heatmap_preselect['cluster_ids'])
    else:
        default_cluster_job = st.session_state.cached_job_ids.get('cluster', '') or ''
        preselected_ids = set()

    # Input for cluster job ID
    cluster_job_id = st.text_input(
        "Cluster Job ID:",
        value=default_cluster_job,
        placeholder="e.g., cluster_20250815_143022_a1b2c3d4",
        help="Enter the job ID from Step 2: Cluster Pockets that you want to use for docking",
        key="docking_cluster_job_id",
    )

    # Input for extract job ID (for PDB source directory)
    extract_job_id = st.text_input(
        "Extract Job ID (optional):",
        placeholder="e.g., extract_20250815_140022_a1b2c3d4",
        help="Enter the job ID from Step 1: Find Pockets. Required if PDB files cannot be auto-detected.",
        key="docking_extract_job_id",
    )

    if cluster_job_id:
        from security import FileValidator, SecurityError
        try:
            cluster_job_id = FileValidator.validate_job_id(cluster_job_id)
        except SecurityError as e:
            st.error(f"Invalid cluster job ID: {e}")
            st.stop()
        # Construct path to cluster representatives file
        representatives_file = os.path.join(RESULTS_DIR, cluster_job_id, "pocket_clusters", "cluster_representatives.csv")

        if os.path.exists(representatives_file):
            st.success(f"✅ Found cluster job: {cluster_job_id}")

            try:
                df_reps = pd.read_csv(representatives_file)
                st.info(f"📊 Cluster has {len(df_reps)} representative pockets")

                # H3: surface what's actually being targeted. If the user came from
                # a heatmap selection, summarize those clusters explicitly; otherwise
                # explain that the default is "top 50% by probability."
                from docking_selection import summarize_selection
                if preselected_ids:
                    _summary = summarize_selection(preselected_ids, df_reps)
                    st.success(
                        f"🎯 **{_summary}.**  \n"
                        "The representatives of these clusters are pre-selected below — "
                        "adjust the checkboxes if you want to add or remove individual frames."
                    )
                elif 'cluster' in df_reps.columns:
                    _all_clusters = sorted(int(c) for c in df_reps['cluster'].dropna().unique())
                    _summary = summarize_selection(_all_clusters, df_reps)
                    st.info(
                        f"🎯 **{_summary}.**  \n"
                        "No heatmap selection was passed in, so the top 50% by probability "
                        "are pre-selected below. Adjust the checkboxes to pick a different subset."
                    )

                # PDB file selection
                st.markdown("### 🎯 Select PDB Files for Docking")
                st.markdown("Choose which PDB files from the cluster you want to include in the docking simulation:")

                # Create checkboxes for each PDB file
                selected_pdbs = []

                # Group by probability for better organization
                df_reps_sorted = df_reps.sort_values('probability', ascending=False)

                # Quick selection buttons
                st.markdown("#### ⚡ Quick Selection")
                col1, col2, col3 = st.columns(3)

                with col1:
                    if st.button("Select All", use_container_width=True):
                        for idx, row in df_reps_sorted.iterrows():
                            key = get_pdb_selection_key(row['File name'], idx, row_id=row.get('Frame_pocket_index'))
                            st.session_state[key] = True
                        st.rerun()

                with col2:
                    if st.button("Select Top 10", use_container_width=True):
                        for i, (idx, row) in enumerate(df_reps_sorted.iterrows()):
                            key = get_pdb_selection_key(row['File name'], idx, row_id=row.get('Frame_pocket_index'))
                            st.session_state[key] = i < 10
                        st.rerun()

                with col3:
                    if st.button("Clear All", use_container_width=True):
                        for idx, row in df_reps_sorted.iterrows():
                            key = get_pdb_selection_key(row['File name'], idx, row_id=row.get('Frame_pocket_index'))
                            st.session_state[key] = False
                        st.rerun()

                # Create columns for better layout
                col1, col2 = st.columns(2)

                def _is_from_heatmap(_row):
                    """True if this row's checkbox state came from a heatmap preselection."""
                    if not preselected_ids:
                        return False
                    _cluster = _row.get('cluster', _row.get('cluster_id', None))
                    return _cluster is not None and pd.notna(_cluster) and int(_cluster) in preselected_ids

                def _row_label(_row, _from_heatmap=False):
                    """Compact human-readable label: 'Cluster N · <spatial> · Prob X.XXX'.

                    Appends a heatmap-source marker when the row's checkbox state was
                    seeded from a Step 2 heatmap selection (H8).
                    """
                    _cluster = _row.get('cluster', _row.get('cluster_id', None))
                    _cluster_part = f"Cluster {int(_cluster)}" if _cluster is not None and pd.notna(_cluster) else "Cluster ?"
                    _spatial = describe_cluster_spatially(_row.get('residues'))
                    _base = f"{_cluster_part} · {_spatial} · Prob {_row['probability']:.3f}"
                    if _from_heatmap:
                        _base += "  ← heatmap"
                    return _base

                def _row_help(_row, _from_heatmap=False):
                    _msg = f"File: {_row['File name']}"
                    if _from_heatmap:
                        _msg += " · pre-selected from your Step 2 heatmap selection"
                    return _msg

                with col1:
                    st.markdown("#### 🏆 High Probability Pockets (Top 50%)")
                    mid = (len(df_reps_sorted) + 1) // 2
                    high_prob_pdbs = df_reps_sorted.iloc[:mid]
                    for pos_idx, (idx, row) in enumerate(high_prob_pdbs.iterrows()):
                        # Stable per-row session_state key (L6)
                        key = get_pdb_selection_key(row['File name'], idx, row_id=row.get('Frame_pocket_index'))
                        _from_heatmap = _is_from_heatmap(row)
                        # Initialize session state if not exists
                        if key not in st.session_state:
                            if preselected_ids:
                                st.session_state[key] = _from_heatmap
                            else:
                                st.session_state[key] = True  # Default to selected for high probability

                        is_selected = st.checkbox(
                            _row_label(row, _from_heatmap),
                            value=st.session_state[key],
                            key=f"{key}_checkbox",
                            help=_row_help(row, _from_heatmap),
                        )
                        st.session_state[key] = is_selected
                        if is_selected:
                            selected_pdbs.append(row)

                with col2:
                    st.markdown("#### 📊 Lower Probability Pockets")
                    low_prob_pdbs = df_reps_sorted.iloc[mid:]
                    for idx, row in low_prob_pdbs.iterrows():
                        # Stable per-row session_state key (L6)
                        key = get_pdb_selection_key(row['File name'], idx, row_id=row.get('Frame_pocket_index'))
                        _from_heatmap = _is_from_heatmap(row)
                        # Initialize session state if not exists
                        if key not in st.session_state:
                            if preselected_ids:
                                st.session_state[key] = _from_heatmap
                            else:
                                st.session_state[key] = False  # Default to not selected for low probability

                        is_selected = st.checkbox(
                            _row_label(row, _from_heatmap),
                            value=st.session_state[key],
                            key=f"{key}_checkbox",
                            help=_row_help(row, _from_heatmap),
                        )
                        st.session_state[key] = is_selected
                        if is_selected:
                            selected_pdbs.append(row)

                # Enforce MAX_DOCKING_PDBS limit
                if len(selected_pdbs) > Config.MAX_DOCKING_PDBS:
                    st.warning(f"⚠️ {len(selected_pdbs)} PDBs selected — capped at {Config.MAX_DOCKING_PDBS} (sorted by probability). Deselect some to remove this cap.")
                    selected_pdbs = sorted(selected_pdbs, key=lambda r: r['probability'] if hasattr(r, '__getitem__') else r.get('probability', 0), reverse=True)[:Config.MAX_DOCKING_PDBS]

                # Store selected PDFs in session state for use when launching docking
                st.session_state.docking_selected_pdbs = selected_pdbs

                # Show selected count
                if selected_pdbs:
                    st.success(f"✅ Selected {len(selected_pdbs)} PDB files for docking")

                    # Show selected files in expandable section
                    with st.expander(f"📋 View Selected PDB Files ({len(selected_pdbs)})"):
                        selected_df = pd.DataFrame(selected_pdbs)
                        if 'residues' in selected_df.columns:
                            selected_df['Location'] = selected_df['residues'].apply(describe_cluster_spatially)
                        _preview_cols = ['File name', 'probability']
                        if 'cluster' in selected_df.columns:
                            selected_df['Cluster'] = selected_df['cluster'].astype('Int64')
                            _preview_cols = ['Cluster', 'Location', 'File name', 'probability']
                        elif 'Location' in selected_df.columns:
                            _preview_cols = ['Location', 'File name', 'probability']
                        st.dataframe(
                            selected_df[_preview_cols].sort_values('probability', ascending=False),
                            use_container_width=True
                        )
                else:
                    st.warning("⚠️ Please select at least one PDB file for docking")

            except Exception as e:
                st.error(f"Error reading cluster representatives: {e}")
                logger.error(f"Error reading cluster representatives: {e}", exc_info=True)
        else:
            st.error(f"❌ Cluster job '{cluster_job_id}' not found or incomplete. Please check the job ID and ensure Step 2: Cluster Pockets has completed successfully.")
            st.info("💡 **Tip:** You can find your cluster job ID in the Task Monitor page or from the Step 2: Cluster Pockets results.")
    else:
        st.info("ℹ️ **Enter a Cluster Job ID** from Step 2: Cluster Pockets to start docking configuration.")
        st.info("💡 **Tip:** You can find your cluster job ID in the Task Monitor page or from the Step 2: Cluster Pockets results.")

    # Ligand upload section
    st.markdown("### 🧪 Ligand Library")
    st.markdown("Upload your ligand library in PDBQT format. You can upload multiple files or a ZIP archive.")

    uploaded_files = st.file_uploader(
        "Upload Ligand Files (PDBQT, SDF, or PDB format)",
        type=['pdbqt', 'sdf', 'pdb', 'zip'],
        accept_multiple_files=True,
        help="Upload PDBQT, SDF, or PDB files. SDF and PDB files will be automatically converted to PDBQT format."
    )

    if uploaded_files:
        # Create job-specific directory for ligands (fixes temp file accumulation)
        ligand_temp_dir = os.path.join(UPLOAD_DIR, f"ligands_{job_id}")
        os.makedirs(ligand_temp_dir, exist_ok=True)

        # Process uploaded files
        ligand_files = []

        for uploaded_file in uploaded_files:
            if uploaded_file.name.endswith('.zip'):
                # Save and validate ZIP file before extraction
                zip_temp_path = Path(ligand_temp_dir) / uploaded_file.name
                with open(zip_temp_path, 'wb') as f:
                    f.write(uploaded_file.getbuffer())

                # Validate ZIP for security threats
                try:
                    FileValidator.validate_zip_file(zip_temp_path)
                    logger.info(f"ZIP file validated: {uploaded_file.name}")
                except SecurityError as e:
                    st.error(f"❌ ZIP file validation failed for {uploaded_file.name}: {e}")
                    logger.error(f"ZIP validation failed: {e}")
                    continue  # Skip this file

                # Safe to extract
                with zipfile.ZipFile(zip_temp_path, 'r') as zip_ref:
                    zip_ref.extractall(ligand_temp_dir)
                    # Find PDBQT files in extracted content
                    pdbqt_count_before = len(ligand_files)
                    for root, dirs, files in os.walk(ligand_temp_dir):
                        for file in files:
                            if file.endswith('.pdbqt'):
                                ligand_files.append(os.path.join(root, file))

                    # Validate ZIP contained PDBQT files
                    pdbqt_found = len(ligand_files) - pdbqt_count_before
                    if pdbqt_found == 0:
                        st.warning(f"⚠️ ZIP file '{uploaded_file.name}' contains no PDBQT files. Please ensure your ligands are in PDBQT format.")
                        logger.warning(f"ZIP {uploaded_file.name} contained no PDBQT files")
            else:
                # Save individual file — sanitize the filename before any I/O.
                try:
                    safe_name = FileValidator.validate_filename(uploaded_file.name)
                except SecurityError as e:
                    st.error(f"Rejected upload `{uploaded_file.name}`: {e}")
                    continue
                file_path = os.path.join(ligand_temp_dir, safe_name)
                with open(file_path, 'wb') as f:
                    f.write(uploaded_file.getbuffer())

                # Convert to PDBQT if needed
                if safe_name.endswith(('.sdf', '.pdb')):
                    try:
                        # Convert using OpenBabel (bounded — won't hang the page).
                        pdbqt_path = file_path.rsplit('.', 1)[0] + '.pdbqt'
                        subprocess.run([
                            'obabel', file_path, '-O', pdbqt_path, '--gen3d'
                        ], check=True, capture_output=True, text=True, timeout=30)

                        # Remove original file and use converted PDBQT
                        os.remove(file_path)
                        ligand_files.append(pdbqt_path)
                        st.success(f"✅ Converted {safe_name} to PDBQT format")
                    except subprocess.TimeoutExpired:
                        st.error(
                            f"❌ Ligand conversion timed out (>30s) for `{safe_name}`. "
                            "Try a simpler structure or pre-convert to PDBQT."
                        )
                    except subprocess.CalledProcessError as e:
                        st.error(f"❌ Failed to convert {safe_name}: {e}")
                        # Keep original file if conversion fails
                        if safe_name.endswith('.pdbqt'):
                            ligand_files.append(file_path)
                else:
                    # Already PDBQT format
                    ligand_files.append(file_path)

        if ligand_files:
            if len(ligand_files) > Config.MAX_DOCKING_LIGANDS:
                st.warning(f"⚠️ {len(ligand_files)} ligand files uploaded — capped at {Config.MAX_DOCKING_LIGANDS}. Only the first {Config.MAX_DOCKING_LIGANDS} will be used.")
                for excess in ligand_files[Config.MAX_DOCKING_LIGANDS:]:
                    try:
                        os.remove(excess)
                    except OSError:
                        pass
                ligand_files = ligand_files[:Config.MAX_DOCKING_LIGANDS]
            st.success(f"✅ Successfully loaded {len(ligand_files)} ligand files")

            # Show sample ligands with preview
            with st.expander(f"📋 View Ligand Library ({len(ligand_files)} compounds)"):
                col1, col2 = st.columns([2, 3])
                with col1:
                    st.markdown("**Sample Ligands:**")
                    sample_ligands = ligand_files[:10]
                    for idx, ligand in enumerate(sample_ligands, 1):
                        st.text(f"{idx}. {os.path.basename(ligand)}")
                    if len(ligand_files) > 10:
                        st.text(f"... and {len(ligand_files) - 10} more")

                with col2:
                    st.markdown("**Preview Ligand Structure:**")
                    selected_ligand = st.selectbox(
                        "Select ligand to preview:",
                        sample_ligands,
                        format_func=lambda x: os.path.basename(x)
                    )
                    if selected_ligand and os.path.exists(selected_ligand):
                        try:
                            with open(selected_ligand, 'r') as f:
                                ligand_data = f.read()
                            show_molecule_3d(None, ligand_data, width=400, height=300, color_scheme=color_scheme, surface_opacity=surface_opacity)
                        except Exception as e:
                            st.error(f"Could not preview ligand: {e}")

            # Start docking button
            st.markdown("---")
            if st.button("🚀 Start Molecular Docking", type="primary", use_container_width=True):
                # Check task rate limit before submission
                try:
                    check_task_rate_limit()
                except RateLimitExceeded as e:
                    st.error(f"⏳ Task submission rate limit exceeded: {e}")
                    st.info(f"Please wait {e.retry_after:.0f} seconds before submitting another task.")
                    logger.warning(f"Task rate limit exceeded for docking job: {e}")
                    st.stop()

                # Get selected PDFs from session state (fixes variable scope bug)
                selected_pdbs = st.session_state.get('docking_selected_pdbs', [])
                if selected_pdbs:
                    # Create filtered representatives file with only selected PDBs
                    selected_df = pd.DataFrame(selected_pdbs)
                    filtered_reps_file = os.path.join(UPLOAD_DIR, f"filtered_reps_{job_id}.csv")
                    selected_df.to_csv(filtered_reps_file, index=False)

                    # Determine PDB source directory
                    pdb_source_dir = None
                    if extract_job_id and extract_job_id.strip():
                        # Use provided extract job ID
                        from security import FileValidator as _FV, SecurityError as _SE
                        try:
                            safe_extract_id = _FV.validate_job_id(extract_job_id.strip())
                        except _SE as e:
                            st.error(f"Invalid extract job ID: {e}")
                            st.stop()
                        pdb_source_dir = os.path.join(RESULTS_DIR, safe_extract_id, "pdbs")
                        if not os.path.exists(pdb_source_dir):
                            st.error(f"❌ PDB directory not found: {pdb_source_dir}")
                            st.stop()
                    else:
                        # Try to auto-detect by searching for extract jobs
                        for dirname in sorted(os.listdir(RESULTS_DIR), reverse=True):
                            if dirname.startswith('extract_'):
                                candidate_dir = os.path.join(RESULTS_DIR, dirname, "pdbs")
                                if os.path.exists(candidate_dir):
                                    pdb_source_dir = candidate_dir
                                    logger.info(f"Auto-detected PDB source: {pdb_source_dir}")
                                    break

                    # Start docking task with all parameters
                    task = run_docking_task.delay(
                        cluster_representatives_csv=filtered_reps_file,
                        ligand_folder=ligand_temp_dir,
                        job_id=job_id,
                        smina_exe_path=smina_path,
                        num_poses=num_poses,
                        exhaustiveness=exhaustiveness,
                        ph_value=ph_value,
                        box_size_x=box_size_x,
                        box_size_y=box_size_y,
                        box_size_z=box_size_z,
                        pdb_source_dir=pdb_source_dir
                    )

                    st.session_state.docking_job_id = job_id
                    st.session_state.docking_task_id = task.id

                    st.success(f"🎯 Docking job started!")
                    st.info(f"**Job ID:** `{job_id}`")
                    st.info("💡 **Switch to the Results & Analysis tab** to monitor progress!")
                    st.info(f"📊 **Parameters:** {len(selected_pdbs)} PDB files, {len(ligand_files)} ligands, {num_poses} poses, exhaustiveness {exhaustiveness}")

                    # Auto-refresh
                    time.sleep(2)
                    st.rerun()
                else:
                    st.error("❌ Please select at least one PDB file for docking")

# Progress and Results Section with integrated 3D Viewer
with tab_results:
    # Option to load existing results
    render_load_previous_widget(
        session_key="docking_job_id",
        label="📂 Load Docking Results",
        placeholder="e.g., docking_20250815_143022_a1b2c3d4",
        text_input_key="docking_load_job_id",
        button_key="docking_load_btn",
    )

    # Show progress or results
    if st.session_state.docking_job_id and st.session_state.docking_task_id:
        # Get task status
        task = celery_app.AsyncResult(st.session_state.docking_task_id)

        if task.state == 'PENDING':
            st.markdown("### 📈 Job Progress")
            st.info("⏳ Docking task is pending in queue…")
            st.progress(0)
            time.sleep(3)
            st.rerun()
        elif task.state == 'PROGRESS':
            st.markdown("### 📈 Job Progress")
            info = task.info or {}
            progress = info.get('progress', 0)
            current_step = info.get('current_step', 'Processing…')
            pairs_done = info.get('pairs_done', 0)
            pairs_total = info.get('pairs_total', 0)

            st.progress(progress / 100)
            st.info(f"🔄 {current_step}")

            if pairs_total > 0:
                pair_pct = int(pairs_done / pairs_total * 100)
                col_a, col_b = st.columns(2)
                col_a.metric("Pairs completed", f"{pairs_done} / {pairs_total}")
                col_b.metric("Pair progress", f"{pair_pct}%")

                # Mini bar showing pair-level detail
                st.progress(pairs_done / pairs_total,
                            text=f"{pairs_done}/{pairs_total} receptor–ligand pairs")

                # Warn if stuck at 5% for a long time (receptor prep phase)
                if progress <= 15:
                    st.caption("⏱️ Preparing receptors (converting PDB → PDBQT)…")

            time.sleep(3)
            st.rerun()
        elif task.state == 'SUCCESS':
            st.success("✅ Docking completed successfully!")

            # Display results
            results = task.result
            if isinstance(results, dict):
                # Update job status file to 'completed'
                update_job_status(
                    st.session_state.docking_job_id,
                    'completed',
                    'Molecular docking completed',
                    result_info={
                        'total_poses': results.get('total_docking_poses', 0),
                        'unique_ligands': results.get('unique_ligands', 0),
                        'best_affinity': results.get('best_affinity', 0)
                    }
                )

                # Partial-success callout: surface per-pair failures (if any)
                from failure_view import render_pair_failures_callout
                render_pair_failures_callout(results, st.session_state.docking_job_id)

                # Metrics overview row
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.metric("Total Poses", results.get('total_docking_poses', 0))
                with col2:
                    st.metric("Unique Ligands", results.get('unique_ligands', 0))
                with col3:
                    st.metric("Unique Receptors", results.get('unique_receptors', 0))
                with col4:
                    best_aff = results.get('best_affinity', 0)
                    category, emoji = classify_affinity(best_aff)
                    st.metric(f"Best Affinity {emoji}", f"{best_aff:.2f} kcal/mol")

                # Load results
                results_file = results.get('docking_results_file')
                if results_file and os.path.exists(results_file):
                    df_results = pd.read_csv(results_file)

                    # Validate DataFrame has required data
                    if df_results.empty:
                        st.warning("⚠️ Results file is empty. No docking poses were generated.")
                    elif 'ligand' not in df_results.columns or 'receptor' not in df_results.columns:
                        st.error("❌ Results file is missing required columns (ligand, receptor)")
                    else:
                        # Filter best poses per ligand-receptor pair
                        df_best = df_results.loc[df_results.groupby(['ligand', 'receptor'])['affinity (kcal/mol)'].idxmin()]
                        df_best['affinity_class'] = df_best['affinity (kcal/mol)'].apply(lambda x: classify_affinity(x)[0])
                        df_best['affinity_emoji'] = df_best['affinity (kcal/mol)'].apply(lambda x: classify_affinity(x)[1])

                        st.markdown("---")

                        # ========== MAIN SPLIT VIEW: Results Table + 3D Viewer ==========
                        st.markdown("### 🎯 Results Explorer with 3D Visualization")

                        # Filter controls in a row
                        filter_col1, filter_col2, filter_col3 = st.columns([2, 2, 1])
                        with filter_col1:
                            affinity_filter = st.multiselect(
                                "Filter by Affinity:",
                                options=['excellent', 'good', 'moderate', 'poor'],
                                default=['excellent', 'good'],
                                key="affinity_filter_main"
                            )
                        with filter_col2:
                            top_n = st.slider("Show top N results:", 5, 50, 15, key="top_n_main")
                        with filter_col3:
                            st.markdown("<br>", unsafe_allow_html=True)
                            auto_view = st.checkbox("Auto-view", value=True, help="Automatically show 3D view when selecting a pose")

                        # Apply filters
                        if affinity_filter:
                            df_filtered = df_best[df_best['affinity_class'].isin(affinity_filter)]
                        else:
                            df_filtered = df_best
                        df_display = df_filtered.sort_values('affinity (kcal/mol)').head(top_n)

                        # Split view: Table on left, 3D viewer on right
                        table_col, viewer_col = st.columns([1, 1])

                        with table_col:
                            st.markdown("#### 🏆 Top Docking Poses")

                            # Create a selection table
                            if not df_display.empty:
                                # Select pose for viewing
                                pose_options = df_display.index.tolist()
                                selected_idx = st.selectbox(
                                    "Select pose to view:",
                                    pose_options,
                                    format_func=lambda x: f"{df_display.loc[x, 'affinity_emoji']} {df_display.loc[x, 'ligand']} ↔ {df_display.loc[x, 'receptor']} ({df_display.loc[x, 'affinity (kcal/mol)']:.2f} kcal/mol)",
                                    key="pose_selector_main"
                                )

                                if selected_idx is not None:
                                    st.session_state.selected_pose = df_display.loc[selected_idx].to_dict()

                                # Display table
                                st.dataframe(
                                    df_display[['ligand', 'receptor', 'affinity (kcal/mol)', 'affinity_emoji', 'rmsd l.b.', 'rmsd u.b.']].rename(
                                        columns={'affinity_emoji': '🎯', 'affinity (kcal/mol)': 'Affinity', 'rmsd l.b.': 'RMSD LB', 'rmsd u.b.': 'RMSD UB'}
                                    ),
                                    use_container_width=True,
                                    height=350
                                )
                            else:
                                st.info("No poses match the selected filters.")

                        with viewer_col:
                            st.markdown("#### 🔬 3D Structure Viewer")

                            if 'selected_pose' in st.session_state and st.session_state.selected_pose:
                                pose = st.session_state.selected_pose

                                # Pose info card
                                category, emoji = classify_affinity(pose.get('affinity (kcal/mol)', 0))
                                st.markdown(f"""
                                <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 1rem; border-radius: 10px; color: white; margin-bottom: 1rem;">
                                    <strong>{emoji} {pose.get('ligand', 'N/A')}</strong> ↔ <strong>{pose.get('receptor', 'N/A')}</strong><br>
                                    <span style="font-size: 1.2rem; font-weight: bold;">{pose.get('affinity (kcal/mol)', 0):.2f} kcal/mol</span>
                                    <span style="margin-left: 1rem; font-size: 0.9rem;">RMSD: {pose.get('rmsd l.b.', 0):.2f} / {pose.get('rmsd u.b.', 0):.2f} Å</span>
                                </div>
                                """, unsafe_allow_html=True)

                                # Visualization controls
                                viz_col1, viz_col2 = st.columns(2)
                                with viz_col1:
                                    viz_style = st.selectbox("Style:", ["cartoon", "surface", "stick", "binding site"], key="viz_style_main")
                                with viz_col2:
                                    show_ligand = st.checkbox("Show Ligand", value=True, key="show_ligand_main")

                                # Try to load and display the structure
                                try:
                                    # Load ligand SDF if available and checkbox enabled
                                    ligand_sdf_data = None
                                    if show_ligand:
                                        sdf_path = pose.get('output_sdf')
                                        mode = pose.get('mode')
                                        if sdf_path and mode is not None:
                                            ligand_sdf_data = extract_sdf_model(sdf_path, mode)

                                    # Prefer PDB over PDBQT for better visualization
                                    receptor_pdb_file = pose.get('receptor_pdb_path')
                                    receptor_file = pose.get('receptor_path')

                                    if receptor_pdb_file and os.path.exists(receptor_pdb_file):
                                        with open(receptor_pdb_file, 'r') as f:
                                            receptor_data = f.read()
                                        show_molecule_3d(receptor_data, ligand_sdf_data, width=400, height=350, style_protein=viz_style, color_scheme=color_scheme, surface_opacity=surface_opacity)
                                    elif receptor_file and os.path.exists(receptor_file):
                                        with open(receptor_file, 'r') as f:
                                            receptor_data = f.read()
                                        show_molecule_3d(receptor_data, ligand_sdf_data, width=400, height=350, style_protein=viz_style, color_scheme=color_scheme, surface_opacity=surface_opacity)
                                    else:
                                        # Try to find receptor in docking output
                                        docking_dir = results.get('docking_output_dir')
                                        if docking_dir:
                                            receptor_name = pose.get('receptor', '')
                                            possible_paths = [
                                                os.path.join(docking_dir, f"{receptor_name}"),
                                                os.path.join(docking_dir, f"{receptor_name}.pdb"),
                                                os.path.join(docking_dir, f"{receptor_name}.pdbqt"),
                                            ]
                                            for path in possible_paths:
                                                if os.path.exists(path):
                                                    with open(path, 'r') as f:
                                                        receptor_data = f.read()
                                                    show_molecule_3d(receptor_data, ligand_sdf_data, width=400, height=350, style_protein=viz_style, color_scheme=color_scheme, surface_opacity=surface_opacity)
                                                    break
                                            else:
                                                st.info("📁 Upload a PDB file to visualize:")
                                                demo_file = st.file_uploader("Upload PDB", type=['pdb'], key='viewer_pdb', label_visibility="collapsed")
                                                if demo_file:
                                                    pdb_content = demo_file.getvalue().decode('utf-8')
                                                    show_molecule_3d(pdb_content, ligand_sdf_data, width=400, height=350, style_protein=viz_style, color_scheme=color_scheme, surface_opacity=surface_opacity)
                                        else:
                                            st.warning("⚠️ Structure files not available")
                                except Exception as e:
                                    st.error(f"Error loading structure: {e}")
                                    logger.error(f"3D viewer error: {e}", exc_info=True)
                            else:
                                st.info("👆 Select a pose from the table to view its 3D structure")
                                # Demo upload
                                demo_file = st.file_uploader("Or upload a PDB file:", type=['pdb'], key='demo_viewer_pdb')
                                if demo_file:
                                    pdb_content = demo_file.getvalue().decode('utf-8')
                                    show_molecule_3d(pdb_content, None, width=400, height=350, style_protein="cartoon", color_scheme=color_scheme, surface_opacity=surface_opacity)

                        # ========== Additional Analysis Tabs ==========
                        st.markdown("---")
                        st.markdown("### 📊 Detailed Analysis")

                        analysis_tab1, analysis_tab2, analysis_tab3 = st.tabs(["📈 Statistics", "🗺️ Heatmap", "💾 Download"])

                        with analysis_tab1:
                            col1, col2 = st.columns(2)

                            with col1:
                                # Histogram
                                fig_hist = px.histogram(
                                    df_results,
                                    x='affinity (kcal/mol)',
                                    title='Affinity Distribution',
                                    nbins=30,
                                    color_discrete_sequence=['#667eea']
                                )
                                fig_hist.update_layout(xaxis_title="Affinity (kcal/mol)", yaxis_title="Count", showlegend=False, height=300)
                                st.plotly_chart(fig_hist, use_container_width=True)

                            with col2:
                                # Box plot by ligand
                                fig_box = px.box(
                                    df_results.groupby('ligand').head(5),
                                    x='ligand',
                                    y='affinity (kcal/mol)',
                                    title='Affinity by Ligand',
                                    color_discrete_sequence=['#764ba2']
                                )
                                fig_box.update_layout(xaxis_title="Ligand", yaxis_title="Affinity", showlegend=False, height=300)
                                fig_box.update_xaxes(tickangle=45)
                                st.plotly_chart(fig_box, use_container_width=True)

                            # Statistics row
                            stat_col1, stat_col2, stat_col3, stat_col4 = st.columns(4)
                            with stat_col1:
                                st.metric("Mean", f"{df_results['affinity (kcal/mol)'].mean():.2f}")
                            with stat_col2:
                                st.metric("Median", f"{df_results['affinity (kcal/mol)'].median():.2f}")
                            with stat_col3:
                                st.metric("Std Dev", f"{df_results['affinity (kcal/mol)'].std():.2f}")
                            with stat_col4:
                                st.metric("Best", f"{df_results['affinity (kcal/mol)'].min():.2f}")

                        with analysis_tab2:
                            # Heatmap
                            if len(df_best) > 1:
                                pivot_data = df_best.pivot_table(
                                    values='affinity (kcal/mol)',
                                    index='ligand',
                                    columns='receptor',
                                    aggfunc='min'
                                )

                                fig_heat = go.Figure(data=go.Heatmap(
                                    z=pivot_data.values,
                                    x=pivot_data.columns,
                                    y=pivot_data.index,
                                    colorscale='RdYlGn_r',
                                    text=pivot_data.values,
                                    texttemplate='%{text:.1f}',
                                    textfont={"size": 9},
                                    colorbar=dict(title="kcal/mol")
                                ))
                                fig_heat.update_layout(
                                    title='Ligand-Receptor Affinity Matrix',
                                    height=max(350, len(pivot_data.index) * 25)
                                )
                                st.plotly_chart(fig_heat, use_container_width=True)
                            else:
                                st.info("Need multiple ligand-receptor pairs for heatmap visualization")

                        with analysis_tab3:
                            dl_col1, dl_col2 = st.columns(2)

                            with dl_col1:
                                csv_data = df_results.to_csv(index=False)
                                st.download_button(
                                    label="📥 Full Results (CSV)",
                                    data=csv_data,
                                    file_name=f"docking_results_{st.session_state.docking_job_id}.csv",
                                    mime="text/csv",
                                    use_container_width=True
                                )

                                best_csv = df_best.to_csv(index=False)
                                st.download_button(
                                    label="📥 Best Poses (CSV)",
                                    data=best_csv,
                                    file_name=f"best_poses_{st.session_state.docking_job_id}.csv",
                                    mime="text/csv",
                                    use_container_width=True
                                )

                            with dl_col2:
                                docking_dir = results.get('docking_output_dir')
                                if docking_dir:
                                    if st.button("🔄 Generate ZIP Archive", use_container_width=True):
                                        with st.spinner("Creating archive..."):
                                            zip_path = os.path.join(docking_dir, 'results.zip')
                                            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                                                for root, dirs, files in os.walk(docking_dir):
                                                    for file in files:
                                                        if file.endswith(('.csv', '.sdf', '.pdbqt', '.log')):
                                                            file_path = os.path.join(root, file)
                                                            zipf.write(file_path, os.path.relpath(file_path, docking_dir))
                                            st.success("✅ Archive created!")

                                    zip_path = os.path.join(docking_dir, 'results.zip')
                                    if os.path.exists(zip_path):
                                        with open(zip_path, 'rb') as f:
                                            st.download_button(
                                                label="📥 Download ZIP",
                                                data=f.read(),
                                                file_name=f"docking_{st.session_state.docking_job_id}.zip",
                                                mime="application/zip",
                                                use_container_width=True
                                            )

        elif task.state == 'FAILURE':
            from failure_view import load_status_json, render_task_failure
            _info = task.info if isinstance(task.info, dict) else {}
            render_task_failure(
                _info,
                load_status_json(st.session_state.docking_job_id),
                st.session_state.docking_job_id,
            )
    elif st.session_state.docking_job_id and not st.session_state.docking_task_id:
        # Load results directly from disk (no Celery task ID — e.g. loaded by job ID)
        from security import FileValidator as _FV2, SecurityError as _SE2
        try:
            _safe_dj_id = _FV2.validate_job_id(st.session_state.docking_job_id)
        except _SE2 as e:
            st.error(f"Invalid docking job ID: {e}")
            st.stop()
        docking_output_dir = os.path.join(RESULTS_DIR, f'dock_{_safe_dj_id}')
        results_file = os.path.join(docking_output_dir, 'docking_results.csv')
        if os.path.exists(results_file):
            st.success("✅ Loaded docking results from disk")
            df_results = pd.read_csv(results_file)

            if df_results.empty:
                st.warning("⚠️ Results file is empty. No docking poses were generated.")
            elif 'ligand' not in df_results.columns or 'receptor' not in df_results.columns:
                st.error("❌ Results file is missing required columns (ligand, receptor)")
            else:
                # Metrics
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.metric("Total Poses", len(df_results))
                with col2:
                    st.metric("Unique Ligands", df_results['ligand'].nunique())
                with col3:
                    st.metric("Unique Receptors", df_results['receptor'].nunique())
                with col4:
                    best_aff = df_results['affinity (kcal/mol)'].min()
                    category, emoji = classify_affinity(best_aff)
                    st.metric(f"Best Affinity {emoji}", f"{best_aff:.2f} kcal/mol")

                # Best poses
                df_best = df_results.loc[df_results.groupby(['ligand', 'receptor'])['affinity (kcal/mol)'].idxmin()]
                df_best['affinity_class'] = df_best['affinity (kcal/mol)'].apply(lambda x: classify_affinity(x)[0])
                df_best['affinity_emoji'] = df_best['affinity (kcal/mol)'].apply(lambda x: classify_affinity(x)[1])

                st.markdown("---")
                st.markdown("### 🎯 Results Explorer with 3D Visualization")

                # Filter controls
                filter_col1, filter_col2, filter_col3 = st.columns([2, 2, 1])
                with filter_col1:
                    affinity_filter = st.multiselect(
                        "Filter by Affinity:",
                        options=['excellent', 'good', 'moderate', 'poor'],
                        default=['excellent', 'good', 'moderate'],
                        key="affinity_filter_loaded"
                    )
                with filter_col2:
                    top_n = st.slider("Show top N results:", 5, 50, 15, key="top_n_loaded")
                with filter_col3:
                    st.markdown("<br>", unsafe_allow_html=True)
                    auto_view = st.checkbox("Auto-view", value=True, key="auto_view_loaded")

                if affinity_filter:
                    df_filtered = df_best[df_best['affinity_class'].isin(affinity_filter)]
                else:
                    df_filtered = df_best
                df_display = df_filtered.sort_values('affinity (kcal/mol)').head(top_n)

                # Split view
                table_col, viewer_col = st.columns([1, 1])

                with table_col:
                    st.markdown("#### 🏆 Top Docking Poses")
                    if not df_display.empty:
                        pose_options = df_display.index.tolist()
                        selected_idx = st.selectbox(
                            "Select pose to view:",
                            pose_options,
                            format_func=lambda x: f"{df_display.loc[x, 'affinity_emoji']} {df_display.loc[x, 'ligand']} ↔ {df_display.loc[x, 'receptor']} ({df_display.loc[x, 'affinity (kcal/mol)']:.2f} kcal/mol)",
                            key="pose_selector_loaded"
                        )
                        if selected_idx is not None:
                            st.session_state.selected_pose = df_display.loc[selected_idx].to_dict()

                        st.dataframe(
                            df_display[['ligand', 'receptor', 'affinity (kcal/mol)', 'affinity_emoji', 'rmsd l.b.', 'rmsd u.b.']].rename(
                                columns={'affinity_emoji': '🎯', 'affinity (kcal/mol)': 'Affinity', 'rmsd l.b.': 'RMSD LB', 'rmsd u.b.': 'RMSD UB'}
                            ),
                            use_container_width=True,
                            height=350
                        )
                    else:
                        st.info("No poses match the selected filters.")

                with viewer_col:
                    st.markdown("#### 🔬 3D Structure Viewer")
                    if 'selected_pose' in st.session_state and st.session_state.selected_pose:
                        pose = st.session_state.selected_pose
                        category, emoji = classify_affinity(pose.get('affinity (kcal/mol)', 0))
                        st.markdown(f"""
                        <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 1rem; border-radius: 10px; color: white; margin-bottom: 1rem;">
                            <strong>{emoji} {pose.get('ligand', 'N/A')}</strong> ↔ <strong>{pose.get('receptor', 'N/A')}</strong><br>
                            <span style="font-size: 1.2rem; font-weight: bold;">{pose.get('affinity (kcal/mol)', 0):.2f} kcal/mol</span>
                            <span style="margin-left: 1rem; font-size: 0.9rem;">RMSD: {pose.get('rmsd l.b.', 0):.2f} / {pose.get('rmsd u.b.', 0):.2f} Å</span>
                        </div>
                        """, unsafe_allow_html=True)

                        viz_col1, viz_col2 = st.columns(2)
                        with viz_col1:
                            viz_style = st.selectbox("Style:", ["cartoon", "surface", "stick", "binding site"], key="viz_style_loaded")
                        with viz_col2:
                            show_ligand = st.checkbox("Show Ligand", value=True, key="show_ligand_loaded")

                        try:
                            ligand_sdf_data = None
                            if show_ligand:
                                sdf_path = pose.get('output_sdf')
                                mode = pose.get('mode')
                                if sdf_path and mode is not None:
                                    ligand_sdf_data = extract_sdf_model(sdf_path, mode)

                            receptor_pdb_file = pose.get('receptor_pdb_path')
                            receptor_file = pose.get('receptor_path')

                            if receptor_pdb_file and os.path.exists(receptor_pdb_file):
                                with open(receptor_pdb_file, 'r') as f:
                                    receptor_data = f.read()
                                show_molecule_3d(receptor_data, ligand_sdf_data, width=400, height=350, style_protein=viz_style, color_scheme=color_scheme, surface_opacity=surface_opacity)
                            elif receptor_file and os.path.exists(receptor_file):
                                with open(receptor_file, 'r') as f:
                                    receptor_data = f.read()
                                show_molecule_3d(receptor_data, ligand_sdf_data, width=400, height=350, style_protein=viz_style, color_scheme=color_scheme, surface_opacity=surface_opacity)
                            else:
                                possible_paths = [
                                    os.path.join(docking_output_dir, f"{pose.get('receptor', '')}"),
                                    os.path.join(docking_output_dir, f"{pose.get('receptor', '')}.pdb"),
                                    os.path.join(docking_output_dir, f"{pose.get('receptor', '')}.pdbqt"),
                                ]
                                for path in possible_paths:
                                    if os.path.exists(path):
                                        with open(path, 'r') as f:
                                            receptor_data = f.read()
                                        show_molecule_3d(receptor_data, ligand_sdf_data, width=400, height=350, style_protein=viz_style, color_scheme=color_scheme, surface_opacity=surface_opacity)
                                        break
                                else:
                                    st.info("📁 Upload a PDB file to visualize:")
                                    demo_file = st.file_uploader("Upload PDB", type=['pdb'], key='viewer_pdb_loaded', label_visibility="collapsed")
                                    if demo_file:
                                        pdb_content = demo_file.getvalue().decode('utf-8')
                                        show_molecule_3d(pdb_content, ligand_sdf_data, width=400, height=350, style_protein=viz_style, color_scheme=color_scheme, surface_opacity=surface_opacity)
                        except Exception as e:
                            st.error(f"Error loading structure: {e}")
                            logger.error(f"3D viewer error: {e}", exc_info=True)
                    else:
                        st.info("👆 Select a pose from the table to view its 3D structure")
        else:
            st.error(f"❌ No results found for job '{st.session_state.docking_job_id}'. Check that the job ID is correct and docking has completed.")
            st.info(f"Looking for: {results_file}")
    else:
        st.info("ℹ️ No active docking job. Start a new job in the 'Setup & Launch' tab or enter a job ID above.")

# Footer
st.markdown("---")
st.markdown("""
<div style='text-align: center; color: #666; padding: 1rem 0;'>
    <p>🔬 Molecular Docking powered by <strong>SMINA</strong> | 3D Visualization by <strong>py3Dmol</strong></p>
    <p style='font-size: 0.85rem; margin-top: 0.5rem;'>
        💡 Lower (more negative) affinity values indicate stronger binding
    </p>
</div>
""", unsafe_allow_html=True)
