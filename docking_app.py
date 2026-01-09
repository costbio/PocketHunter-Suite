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
import glob
import subprocess
from pathlib import Path
from tasks import run_docking_task
from celery_app import celery_app
from config import Config
from security import FileValidator, SecurityError
from logging_config import setup_logging
import py3Dmol
import streamlit.components.v1 as components

# Use Config for directories
RESULTS_DIR = str(Config.RESULTS_DIR)
UPLOAD_DIR = str(Config.UPLOAD_DIR)

# Setup logging
logger = setup_logging(__name__)

# Page configuration is handled by main.py

# Custom CSS for docking page with enhanced styling
st.markdown("""
<style>
    .docking-header {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 50%, #f093fb 100%);
        padding: 2rem;
        border-radius: 20px;
        margin-bottom: 2rem;
        color: white;
        text-align: center;
        box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        border: 1px solid rgba(255, 255, 255, 0.18);
    }

    .docking-header h1 {
        margin: 0;
        font-size: 2.5rem;
        font-weight: 700;
        text-shadow: 2px 2px 4px rgba(0,0,0,0.1);
    }

    .docking-card {
        background: rgba(255, 255, 255, 0.95);
        backdrop-filter: blur(10px);
        padding: 2rem;
        border-radius: 15px;
        box-shadow: 0 8px 32px rgba(31, 38, 135, 0.15);
        border: 1px solid rgba(255, 255, 255, 0.18);
        margin: 1.5rem 0;
        transition: all 0.3s ease;
    }

    .docking-card:hover {
        transform: translateY(-2px);
        box-shadow: 0 12px 48px rgba(31, 38, 135, 0.2);
    }

    .ligand-upload {
        border: 3px dashed #667eea;
        border-radius: 20px;
        padding: 2.5rem;
        text-align: center;
        background: linear-gradient(135deg, #f5f7fa 0%, #e8ebf5 100%);
        margin: 1.5rem 0;
        transition: all 0.3s ease;
    }

    .ligand-upload:hover {
        border-color: #764ba2;
        background: linear-gradient(135deg, #e8ebf5 0%, #dce1f0 100%);
        transform: scale(1.01);
    }

    .results-table {
        background: white;
        border-radius: 15px;
        overflow: hidden;
        box-shadow: 0 4px 20px rgba(0,0,0,0.08);
    }

    .affinity-badge {
        display: inline-block;
        padding: 0.4rem 1rem;
        border-radius: 25px;
        font-size: 0.85rem;
        font-weight: 600;
        text-align: center;
        box-shadow: 0 2px 8px rgba(0,0,0,0.1);
    }

    .affinity-excellent {
        background: linear-gradient(135deg, #00b894 0%, #00a085 100%);
        color: white;
    }

    .affinity-good {
        background: linear-gradient(135deg, #55efc4 0%, #00b894 100%);
        color: white;
    }

    .affinity-moderate {
        background: linear-gradient(135deg, #fdcb6e 0%, #e17055 100%);
        color: white;
    }

    .affinity-poor {
        background: linear-gradient(135deg, #ff7675 0%, #d63031 100%);
        color: white;
    }

    .job-id-display {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        padding: 1.2rem;
        border-radius: 15px;
        font-family: 'Courier New', monospace;
        font-size: 1.1rem;
        text-align: center;
        margin: 1.5rem 0;
        box-shadow: 0 4px 20px rgba(102, 126, 234, 0.3);
        border: 1px solid rgba(255, 255, 255, 0.2);
    }

    .metric-card {
        background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
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
        color: #667eea;
        margin: 0.5rem 0;
    }

    .metric-label {
        font-size: 0.9rem;
        color: #666;
        text-transform: uppercase;
        letter-spacing: 1px;
    }

    /* Tabs styling */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
    }

    .stTabs [data-baseweb="tab"] {
        height: 50px;
        background-color: #f5f7fa;
        border-radius: 10px;
        padding: 0 24px;
        font-weight: 600;
    }

    .stTabs [aria-selected="true"] {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
    }

    /* 3D viewer container */
    .viewer-container {
        border-radius: 15px;
        overflow: hidden;
        box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        margin: 1rem 0;
    }
</style>
""", unsafe_allow_html=True)

# Header
st.markdown("""
<div class="docking-header">
    <h1>🔬 Molecular Docking Suite</h1>
    <p style="font-size: 1.2rem; margin-top: 0.5rem;">Advanced ligand-protein docking with 3D visualization and analysis</p>
</div>
""", unsafe_allow_html=True)

# Initialize session state
if 'docking_job_id' not in st.session_state:
    st.session_state.docking_job_id = None
if 'docking_task_id' not in st.session_state:
    st.session_state.docking_task_id = None
if 'view_mode' not in st.session_state:
    st.session_state.view_mode = 'setup'

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
        max_value=20,
        value=8,
        help="Accuracy of docking calculations (higher = more accurate but slower)"
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

    box_size_x = st.slider(
        "Box Size X (Å)",
        min_value=10.0,
        max_value=50.0,
        value=20.0,
        step=1.0,
        help="Size of docking box in X direction"
    )

    box_size_y = st.slider(
        "Box Size Y (Å)",
        min_value=10.0,
        max_value=50.0,
        value=20.0,
        step=1.0,
        help="Size of docking box in Y direction"
    )

    box_size_z = st.slider(
        "Box Size Z (Å)",
        min_value=10.0,
        max_value=50.0,
        value=20.0,
        step=1.0,
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

# Function to display 3D molecule using py3Dmol
def show_molecule_3d(pdb_data, sdf_data=None, width=800, height=600, style_protein="cartoon", style_ligand="stick"):
    """
    Display 3D molecular structure using py3Dmol

    Args:
        pdb_data: PDB file content as string
        sdf_data: SDF/PDBQT file content as string (optional, for ligand)
        width: Viewer width
        height: Viewer height
        style_protein: Protein visualization style
        style_ligand: Ligand visualization style
    """
    view = py3Dmol.view(width=width, height=height)

    # Add protein
    if pdb_data:
        view.addModel(pdb_data, 'pdb')
        if style_protein == "cartoon":
            view.setStyle({'model': 0}, {'cartoon': {'color': color_scheme}})
        elif style_protein == "surface":
            view.setStyle({'model': 0}, {'surface': {'opacity': surface_opacity, 'color': color_scheme}})
        elif style_protein == "stick":
            view.setStyle({'model': 0}, {'stick': {'colorscheme': color_scheme}})

    # Add ligand if provided
    if sdf_data:
        view.addModel(sdf_data, 'sdf')
        view.setStyle({'model': 1}, {'stick': {'colorscheme': 'greenCarbon', 'radius': 0.2}, 'sphere': {'scale': 0.3}})

    view.zoomTo()
    view.spin(False)

    # Generate HTML
    html = f"""
    <div class="viewer-container">
        {view._make_html()}
    </div>
    """
    components.html(html, height=height+50, scrolling=False)

# Function to classify affinity
def classify_affinity(affinity):
    """Classify binding affinity into categories"""
    if affinity < -10:
        return "excellent", "🟢"
    elif affinity < -8:
        return "good", "🟡"
    elif affinity < -6:
        return "moderate", "🟠"
    else:
        return "poor", "🔴"

# Main content area - Create tabs for different views
tab_setup, tab_results, tab_viewer = st.tabs(["🎯 Setup & Launch", "📊 Results & Analysis", "🔬 3D Viewer"])

with tab_setup:
    st.markdown("### 🎯 Job Configuration")

    # Generate job ID once and store in session state
    if 'docking_display_job_id' not in st.session_state:
        st.session_state.docking_display_job_id = f"docking_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}"
    job_id = st.session_state.docking_display_job_id

    # Display job ID with reset button
    col_id, col_reset = st.columns([4, 1])
    with col_id:
        st.markdown(f"""
        <div class="job-id-display">
            <strong>🎯 Job ID:</strong> {job_id}
        </div>
        """, unsafe_allow_html=True)
    with col_reset:
        if st.button("🔄 New Job", help="Generate a new job ID for a fresh docking configuration"):
            st.session_state.docking_display_job_id = f"docking_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}"
            st.rerun()

    st.info("💡 **Save this Job ID** - you can use it to monitor progress in the Task Monitor page!")

    # Cluster selection
    st.markdown("### 📁 Select Cluster Results")

    # Input for cluster job ID
    cluster_job_id = st.text_input(
        "Cluster Job ID:",
        placeholder="e.g., cluster_20250815_143022_a1b2c3d4",
        help="Enter the job ID from Step 3: Cluster Pockets that you want to use for docking"
    )

    if cluster_job_id:
        # Construct path to cluster representatives file
        representatives_file = os.path.join(RESULTS_DIR, cluster_job_id, "pocket_clusters", "cluster_representatives.csv")

        if os.path.exists(representatives_file):
            st.success(f"✅ Found cluster job: {cluster_job_id}")

            try:
                df_reps = pd.read_csv(representatives_file)
                st.info(f"📊 Cluster has {len(df_reps)} representative pockets")

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
                            st.session_state[f"pdb_{idx}"] = True
                        st.rerun()

                with col2:
                    if st.button("Select Top 10", use_container_width=True):
                        for idx, row in df_reps_sorted.iterrows():
                            st.session_state[f"pdb_{idx}"] = idx < 10
                        st.rerun()

                with col3:
                    if st.button("Clear All", use_container_width=True):
                        for idx, row in df_reps_sorted.iterrows():
                            st.session_state[f"pdb_{idx}"] = False
                        st.rerun()

                # Create columns for better layout
                col1, col2 = st.columns(2)

                with col1:
                    st.markdown("#### 🏆 High Probability Pockets (Top 50%)")
                    high_prob_pdbs = df_reps_sorted.head(len(df_reps_sorted)//2)
                    for idx, row in high_prob_pdbs.iterrows():
                        # Initialize session state if not exists
                        if f"pdb_{idx}" not in st.session_state:
                            st.session_state[f"pdb_{idx}"] = True  # Default to selected for high probability

                        is_selected = st.checkbox(
                            f"{row['File name']} (Prob: {row['probability']:.3f})",
                            value=st.session_state[f"pdb_{idx}"],
                            key=f"pdb_{idx}_high"
                        )
                        st.session_state[f"pdb_{idx}"] = is_selected
                        if is_selected:
                            selected_pdbs.append(row)

                with col2:
                    st.markdown("#### 📊 Lower Probability Pockets")
                    low_prob_pdbs = df_reps_sorted.tail(len(df_reps_sorted)//2)
                    for idx, row in low_prob_pdbs.iterrows():
                        # Initialize session state if not exists
                        if f"pdb_{idx}" not in st.session_state:
                            st.session_state[f"pdb_{idx}"] = False  # Default to not selected for low probability

                        is_selected = st.checkbox(
                            f"{row['File name']} (Prob: {row['probability']:.3f})",
                            value=st.session_state[f"pdb_{idx}"],
                            key=f"pdb_{idx}_low"
                        )
                        st.session_state[f"pdb_{idx}"] = is_selected
                        if is_selected:
                            selected_pdbs.append(row)

                # Show selected count
                if selected_pdbs:
                    st.success(f"✅ Selected {len(selected_pdbs)} PDB files for docking")

                    # Show selected files in expandable section
                    with st.expander(f"📋 View Selected PDB Files ({len(selected_pdbs)})"):
                        selected_df = pd.DataFrame(selected_pdbs)
                        st.dataframe(
                            selected_df[['File name', 'residues', 'probability']].sort_values('probability', ascending=False),
                            use_container_width=True
                        )
                else:
                    st.warning("⚠️ Please select at least one PDB file for docking")

            except Exception as e:
                st.error(f"Error reading cluster representatives: {e}")
                logger.error(f"Error reading cluster representatives: {e}", exc_info=True)
        else:
            st.error(f"❌ Cluster job '{cluster_job_id}' not found or incomplete. Please check the job ID and ensure Step 3: Cluster Pockets has completed successfully.")
            st.info("💡 **Tip:** You can find your cluster job ID in the Task Monitor page or from the Step 3: Cluster Pockets results.")
    else:
        st.info("ℹ️ **Enter a Cluster Job ID** from Step 3: Cluster Pockets to start docking configuration.")
        st.info("💡 **Tip:** You can find your cluster job ID in the Task Monitor page or from the Step 3: Cluster Pockets results.")

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
        # Create temporary directory for ligands
        ligand_temp_dir = os.path.join(UPLOAD_DIR, "ligands_temp")
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
                    for root, dirs, files in os.walk(ligand_temp_dir):
                        for file in files:
                            if file.endswith('.pdbqt'):
                                ligand_files.append(os.path.join(root, file))
            else:
                # Save individual file
                file_path = os.path.join(ligand_temp_dir, uploaded_file.name)
                with open(file_path, 'wb') as f:
                    f.write(uploaded_file.getbuffer())

                # Convert to PDBQT if needed
                if uploaded_file.name.endswith(('.sdf', '.pdb')):
                    try:
                        # Convert using OpenBabel
                        pdbqt_path = file_path.rsplit('.', 1)[0] + '.pdbqt'
                        subprocess.run([
                            'obabel', file_path, '-O', pdbqt_path, '--gen3d'
                        ], check=True, capture_output=True, text=True)

                        # Remove original file and use converted PDBQT
                        os.remove(file_path)
                        ligand_files.append(pdbqt_path)
                        st.success(f"✅ Converted {uploaded_file.name} to PDBQT format")
                    except subprocess.CalledProcessError as e:
                        st.error(f"❌ Failed to convert {uploaded_file.name}: {e}")
                        # Keep original file if conversion fails
                        if uploaded_file.name.endswith('.pdbqt'):
                            ligand_files.append(file_path)
                else:
                    # Already PDBQT format
                    ligand_files.append(file_path)

        if ligand_files:
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
                            show_molecule_3d(None, ligand_data, width=400, height=300)
                        except Exception as e:
                            st.error(f"Could not preview ligand: {e}")

            # Start docking button
            st.markdown("---")
            if st.button("🚀 Start Molecular Docking", type="primary", use_container_width=True):
                if 'selected_pdbs' in locals() and selected_pdbs:
                    # Create filtered representatives file with only selected PDBs
                    selected_df = pd.DataFrame(selected_pdbs)
                    filtered_reps_file = os.path.join(UPLOAD_DIR, f"filtered_reps_{job_id}.csv")
                    selected_df.to_csv(filtered_reps_file, index=False)

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
                        box_size_z=box_size_z
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

# Progress and Results Section
with tab_results:
    # Option to load existing results
    st.markdown("### 📂 Load Docking Results")

    col1, col2 = st.columns([3, 1])
    with col1:
        load_job_id = st.text_input(
            "Enter Docking Job ID:",
            value=st.session_state.docking_job_id if st.session_state.docking_job_id else "",
            placeholder="e.g., docking_20250815_143022_a1b2c3d4",
            help="Enter a docking job ID to view its results"
        )
    with col2:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("🔍 Load Results", use_container_width=True):
            if load_job_id:
                st.session_state.docking_job_id = load_job_id
                st.rerun()

    # Show progress or results
    if st.session_state.docking_job_id and st.session_state.docking_task_id:
        st.markdown("### 📈 Current Job Progress")

        # Get task status
        task = celery_app.AsyncResult(st.session_state.docking_task_id)

        if task.state == 'PENDING':
            st.info("⏳ Task is pending in queue...")
            if st.button("🔄 Refresh Status"):
                st.rerun()
        elif task.state == 'PROGRESS':
            progress_data = task.info
            if isinstance(progress_data, dict):
                progress = progress_data.get('progress', 0)
                current_step = progress_data.get('current_step', 'Processing...')
                status = progress_data.get('status', 'Running...')

                st.progress(progress / 100)
                st.info(f"🔄 {current_step}")
                st.write(f"**Status:** {status}")

                # Auto-refresh for progress updates
                if progress < 100:
                    if st.button("🔄 Refresh Progress"):
                        st.rerun()
                else:
                    st.success("✅ Docking completed!")
            else:
                st.warning("⚠️ Progress data format unexpected")
        elif task.state == 'SUCCESS':
            st.success("✅ Docking completed successfully!")

            # Display results
            results = task.result
            if isinstance(results, dict):
                st.markdown("### 📊 Docking Results Overview")

                # Metrics in cards
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.markdown(f"""
                    <div class="metric-card">
                        <div class="metric-label">Total Poses</div>
                        <div class="metric-value">{results.get('total_docking_poses', 0)}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col2:
                    st.markdown(f"""
                    <div class="metric-card">
                        <div class="metric-label">Unique Ligands</div>
                        <div class="metric-value">{results.get('unique_ligands', 0)}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col3:
                    st.markdown(f"""
                    <div class="metric-card">
                        <div class="metric-label">Unique Receptors</div>
                        <div class="metric-value">{results.get('unique_receptors', 0)}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col4:
                    best_aff = results.get('best_affinity', 0)
                    category, emoji = classify_affinity(best_aff)
                    st.markdown(f"""
                    <div class="metric-card">
                        <div class="metric-label">Best Affinity {emoji}</div>
                        <div class="metric-value">{best_aff:.2f}</div>
                        <div style="font-size: 0.8rem; color: #666;">kcal/mol</div>
                    </div>
                    """, unsafe_allow_html=True)

                # Load and display results
                results_file = results.get('docking_results_file')
                if results_file and os.path.exists(results_file):
                    df_results = pd.read_csv(results_file)

                    # Filter best poses per ligand-receptor pair
                    df_best = df_results.loc[df_results.groupby(['ligand', 'receptor'])['affinity (kcal/mol)'].idxmin()]

                    # Add affinity classification
                    df_best['affinity_class'] = df_best['affinity (kcal/mol)'].apply(lambda x: classify_affinity(x)[0])
                    df_best['affinity_emoji'] = df_best['affinity (kcal/mol)'].apply(lambda x: classify_affinity(x)[1])

                    # Create sub-tabs for different analyses
                    results_tab1, results_tab2, results_tab3, results_tab4 = st.tabs([
                        "🏆 Best Poses",
                        "📈 Affinity Analysis",
                        "🗺️ Heatmap",
                        "💾 Download"
                    ])

                    with results_tab1:
                        st.markdown("#### 🏆 Top Docking Poses")

                        # Filter options
                        col1, col2 = st.columns(2)
                        with col1:
                            affinity_filter = st.multiselect(
                                "Filter by Affinity Class:",
                                options=['excellent', 'good', 'moderate', 'poor'],
                                default=['excellent', 'good']
                            )
                        with col2:
                            top_n = st.slider("Show top N results:", 10, 100, 20)

                        if affinity_filter:
                            df_filtered = df_best[df_best['affinity_class'].isin(affinity_filter)]
                        else:
                            df_filtered = df_best

                        df_display = df_filtered.sort_values('affinity (kcal/mol)').head(top_n)

                        # Enhanced table with styling
                        st.dataframe(
                            df_display[['ligand', 'receptor', 'affinity (kcal/mol)', 'affinity_emoji', 'rmsd l.b.', 'rmsd u.b.']].rename(
                                columns={'affinity_emoji': 'Quality'}
                            ),
                            use_container_width=True,
                            height=400
                        )

                        # Selection for 3D viewing
                        st.markdown("---")
                        st.markdown("**Select a pose to view in 3D Viewer tab:**")
                        selected_idx = st.selectbox(
                            "Choose pose:",
                            df_display.index,
                            format_func=lambda x: f"{df_display.loc[x, 'ligand']} - {df_display.loc[x, 'receptor']} ({df_display.loc[x, 'affinity (kcal/mol)']:.2f} kcal/mol)"
                        )
                        if selected_idx is not None:
                            st.session_state.selected_pose = df_display.loc[selected_idx].to_dict()
                            st.info("✅ Pose selected! Switch to the 3D Viewer tab to visualize it.")

                    with results_tab2:
                        st.markdown("#### 📈 Affinity Distribution Analysis")

                        col1, col2 = st.columns(2)

                        with col1:
                            # Histogram
                            fig_hist = px.histogram(
                                df_results,
                                x='affinity (kcal/mol)',
                                title='Distribution of Docking Affinities',
                                nbins=30,
                                color_discrete_sequence=['#667eea']
                            )
                            fig_hist.update_layout(
                                xaxis_title="Affinity (kcal/mol)",
                                yaxis_title="Number of Poses",
                                showlegend=False
                            )
                            st.plotly_chart(fig_hist, use_container_width=True)

                        with col2:
                            # Box plot by ligand
                            fig_box = px.box(
                                df_results.groupby('ligand').head(5),  # Top 5 per ligand
                                x='ligand',
                                y='affinity (kcal/mol)',
                                title='Affinity Range by Ligand',
                                color_discrete_sequence=['#764ba2']
                            )
                            fig_box.update_layout(
                                xaxis_title="Ligand",
                                yaxis_title="Affinity (kcal/mol)",
                                showlegend=False
                            )
                            fig_box.update_xaxes(tickangle=45)
                            st.plotly_chart(fig_box, use_container_width=True)

                        # Statistics
                        st.markdown("#### 📊 Statistical Summary")
                        col1, col2, col3, col4 = st.columns(4)
                        with col1:
                            st.metric("Mean Affinity", f"{df_results['affinity (kcal/mol)'].mean():.2f}")
                        with col2:
                            st.metric("Median Affinity", f"{df_results['affinity (kcal/mol)'].median():.2f}")
                        with col3:
                            st.metric("Std Dev", f"{df_results['affinity (kcal/mol)'].std():.2f}")
                        with col4:
                            st.metric("Min Affinity", f"{df_results['affinity (kcal/mol)'].min():.2f}")

                    with results_tab3:
                        st.markdown("#### 🗺️ Ligand-Receptor Affinity Heatmap")

                        # Create pivot table for heatmap
                        pivot_data = df_best.pivot_table(
                            values='affinity (kcal/mol)',
                            index='ligand',
                            columns='receptor',
                            aggfunc='min'
                        )

                        # Create heatmap
                        fig_heat = go.Figure(data=go.Heatmap(
                            z=pivot_data.values,
                            x=pivot_data.columns,
                            y=pivot_data.index,
                            colorscale='RdYlGn_r',
                            text=pivot_data.values,
                            texttemplate='%{text:.2f}',
                            textfont={"size": 10},
                            colorbar=dict(title="Affinity<br>(kcal/mol)")
                        ))

                        fig_heat.update_layout(
                            title='Binding Affinity Matrix (Lower is Better)',
                            xaxis_title='Receptor',
                            yaxis_title='Ligand',
                            height=max(400, len(pivot_data.index) * 30)
                        )

                        st.plotly_chart(fig_heat, use_container_width=True)

                        # Best combinations
                        st.markdown("#### 🏅 Top 10 Ligand-Receptor Combinations")
                        top_combinations = df_best.nsmallest(10, 'affinity (kcal/mol)')

                        for idx, row in top_combinations.iterrows():
                            category, emoji = classify_affinity(row['affinity (kcal/mol)'])
                            st.markdown(f"""
                            <div class="docking-card">
                                <strong>{emoji} {row['ligand']}</strong> ↔ <strong>{row['receptor']}</strong><br>
                                <span class="affinity-badge affinity-{category}">
                                    {row['affinity (kcal/mol)']:.2f} kcal/mol
                                </span>
                                <span style="margin-left: 1rem; color: #666;">
                                    RMSD: {row['rmsd l.b.']:.2f} / {row['rmsd u.b.']:.2f} Å
                                </span>
                            </div>
                            """, unsafe_allow_html=True)

                    with results_tab4:
                        st.markdown("#### 💾 Download Results")

                        col1, col2 = st.columns(2)

                        with col1:
                            st.markdown("**📄 Download Data Files**")

                            # CSV download
                            csv_data = df_results.to_csv(index=False)
                            st.download_button(
                                label="📥 Download Full Results (CSV)",
                                data=csv_data,
                                file_name=f"docking_results_{st.session_state.docking_job_id}.csv",
                                mime="text/csv",
                                use_container_width=True
                            )

                            # Best poses CSV
                            best_csv = df_best.to_csv(index=False)
                            st.download_button(
                                label="📥 Download Best Poses (CSV)",
                                data=best_csv,
                                file_name=f"best_poses_{st.session_state.docking_job_id}.csv",
                                mime="text/csv",
                                use_container_width=True
                            )

                        with col2:
                            st.markdown("**📦 Download Structure Files**")

                            # Create ZIP with all results
                            if st.button("🔄 Generate Complete Results ZIP", use_container_width=True):
                                with st.spinner("Creating ZIP archive..."):
                                    zip_path = os.path.join(results.get('docking_output_dir'), 'docking_results_complete.zip')
                                    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                                        for root, dirs, files in os.walk(results.get('docking_output_dir')):
                                            for file in files:
                                                if file.endswith(('.csv', '.sdf', '.pdbqt', '.log')):
                                                    file_path = os.path.join(root, file)
                                                    zipf.write(file_path, os.path.relpath(file_path, results.get('docking_output_dir')))
                                    st.success("✅ ZIP archive created!")

                            # Download ZIP
                            zip_path = os.path.join(results.get('docking_output_dir'), 'docking_results_complete.zip')
                            if os.path.exists(zip_path):
                                with open(zip_path, 'rb') as f:
                                    st.download_button(
                                        label="📥 Download Complete ZIP",
                                        data=f.read(),
                                        file_name=f"docking_results_{st.session_state.docking_job_id}.zip",
                                        mime="application/zip",
                                        use_container_width=True
                                    )

                        st.markdown("---")
                        st.info("💡 The ZIP file contains all docking poses in PDBQT/SDF format, CSV result tables, and log files.")

        elif task.state == 'FAILURE':
            st.error("❌ Docking job failed!")
            if isinstance(task.info, dict):
                error_msg = task.info.get('exc_message', 'Unknown error')
            else:
                error_msg = str(task.info) if task.info else 'Unknown error'
            st.error(f"Error: {error_msg}")
            st.info("💡 Check the Task Monitor for detailed error logs.")
    else:
        st.info("ℹ️ No active docking job. Start a new job in the 'Setup & Launch' tab or enter a job ID above to load existing results.")

# 3D Viewer Section
with tab_viewer:
    st.markdown("### 🔬 Interactive 3D Molecular Viewer")

    if 'selected_pose' in st.session_state and st.session_state.selected_pose:
        pose = st.session_state.selected_pose

        st.markdown(f"""
        <div class="docking-card">
            <h4>🎯 Selected Pose</h4>
            <p><strong>Ligand:</strong> {pose.get('ligand', 'N/A')}</p>
            <p><strong>Receptor:</strong> {pose.get('receptor', 'N/A')}</p>
            <p><strong>Affinity:</strong> <span class="affinity-badge affinity-{pose.get('affinity_class', 'poor')}">
                {pose.get('affinity (kcal/mol)', 0):.2f} kcal/mol
            </span></p>
            <p><strong>RMSD:</strong> {pose.get('rmsd l.b.', 0):.2f} / {pose.get('rmsd u.b.', 0):.2f} Å</p>
        </div>
        """, unsafe_allow_html=True)

        # Visualization controls
        col1, col2 = st.columns(2)
        with col1:
            viz_style = st.selectbox(
                "Protein Style:",
                ["cartoon", "surface", "stick"],
                help="Visualization style for the protein structure"
            )
        with col2:
            show_binding_site = st.checkbox("Highlight Binding Site", value=True)

        # Try to load and display the structure
        try:
            # Construct file paths (this would need to match your actual file structure)
            receptor_file = pose.get('receptor_file')  # You'd need to add this to the results
            ligand_file = pose.get('pose_file')  # You'd need to add this to the results

            if receptor_file and os.path.exists(receptor_file):
                with open(receptor_file, 'r') as f:
                    receptor_data = f.read()

                ligand_data = None
                if ligand_file and os.path.exists(ligand_file):
                    with open(ligand_file, 'r') as f:
                        ligand_data = f.read()

                show_molecule_3d(receptor_data, ligand_data, style_protein=viz_style)
            else:
                st.warning("⚠️ Structure files not found. Make sure to save receptor and pose file paths in the results.")
                st.info("💡 File paths need to be added to the docking results CSV for 3D visualization.")

        except Exception as e:
            st.error(f"Error loading structure: {e}")
            logger.error(f"Error loading structure for 3D viewer: {e}", exc_info=True)

    else:
        st.info("ℹ️ No pose selected for viewing. Go to the 'Results & Analysis' tab and select a pose from the Best Poses section.")

        # Show demo viewer
        st.markdown("#### 📺 Demo Viewer")
        st.markdown("Upload a PDB file to preview:")

        demo_file = st.file_uploader("Upload PDB file", type=['pdb'], key='demo_pdb')
        if demo_file:
            pdb_content = demo_file.getvalue().decode('utf-8')
            show_molecule_3d(pdb_content, style_protein="cartoon")

# Footer
st.markdown("---")
st.markdown("""
<div style='text-align: center; color: #666;'>
    <p>🔬 Molecular Docking powered by SMINA | 3D Visualization by py3Dmol | Part of the PocketHunter Suite</p>
    <p style='font-size: 0.85rem; margin-top: 0.5rem;'>
        💡 Tip: Lower (more negative) affinity values indicate stronger binding
    </p>
</div>
""", unsafe_allow_html=True)
