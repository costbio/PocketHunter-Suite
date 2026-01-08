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

# Use Config for directories
RESULTS_DIR = str(Config.RESULTS_DIR)
UPLOAD_DIR = str(Config.UPLOAD_DIR)

# Setup logging
logger = setup_logging(__name__)

# Page configuration is handled by main.py

# Custom CSS for docking page
st.markdown("""
<style>
    .docking-header {
        background: linear-gradient(90deg, #ff6b6b 0%, #ee5a24 100%);
        padding: 1.5rem;
        border-radius: 15px;
        margin-bottom: 2rem;
        color: white;
        text-align: center;
        box-shadow: 0 4px 15px rgba(0,0,0,0.1);
    }
    .docking-card {
        background: white;
        padding: 2rem;
        border-radius: 15px;
        box-shadow: 0 4px 15px rgba(0,0,0,0.1);
        border-left: 5px solid #ff6b6b;
        margin: 1.5rem 0;
    }
    .ligand-upload {
        border: 3px dashed #ff6b6b;
        border-radius: 15px;
        padding: 2rem;
        text-align: center;
        background: linear-gradient(135deg, #fff5f5 0%, #ffe8e8 100%);
        margin: 1rem 0;
        transition: all 0.3s ease;
    }
    .ligand-upload:hover {
        border-color: #ee5a24;
        background: linear-gradient(135deg, #ffe8e8 0%, #ffd8d8 100%);
    }
    .results-table {
        background: white;
        border-radius: 10px;
        overflow: hidden;
        box-shadow: 0 2px 10px rgba(0,0,0,0.1);
    }
    .affinity-badge {
        background: linear-gradient(90deg, #00b894 0%, #00a085 100%);
        color: white;
        padding: 0.3rem 0.8rem;
        border-radius: 20px;
        font-size: 0.8rem;
        font-weight: bold;
    }
    .affinity-badge.poor {
        background: linear-gradient(90deg, #ff7675 0%, #d63031 100%);
    }
    .affinity-badge.moderate {
        background: linear-gradient(90deg, #fdcb6e 0%, #e17055 100%);
    }
    .job-id-display {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        padding: 1rem;
        border-radius: 10px;
        font-family: monospace;
        font-size: 1.1rem;
        text-align: center;
        margin: 1rem 0;
        box-shadow: 0 4px 15px rgba(0,0,0,0.1);
    }
</style>
""", unsafe_allow_html=True)

# Header
st.markdown("""
<div class="docking-header">
    <h1>🔬 Molecular Docking</h1>
    <p>Dock ligands to pocket cluster representatives using SMINA</p>
</div>
""", unsafe_allow_html=True)

# Initialize session state
if 'docking_job_id' not in st.session_state:
    st.session_state.docking_job_id = None
if 'docking_task_id' not in st.session_state:
    st.session_state.docking_task_id = None

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

# Main content area
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
st.markdown("### 📁 Enter Cluster Job ID")

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
        
        # Show sample ligands
        with st.expander("View Sample Ligands"):
            sample_ligands = ligand_files[:5]
            for ligand in sample_ligands:
                st.text(os.path.basename(ligand))
        
        # Start docking button
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
                st.info("💡 **Save this Job ID** - you can use it to monitor progress in the Task Monitor page!")
                st.info(f"📊 **Parameters:** {len(selected_pdbs)} PDB files, {len(ligand_files)} ligands, {num_poses} poses, exhaustiveness {exhaustiveness}")
                
                # Auto-refresh
                st.rerun()
            else:
                st.error("❌ Please select at least one PDB file for docking")

# Progress and Results Section (only for current session job)
if st.session_state.docking_job_id and st.session_state.docking_task_id:
    st.markdown("### 📈 Current Job Progress")
    
    # Get task status
    task = celery_app.AsyncResult(st.session_state.docking_task_id)
    
    if task.state == 'PENDING':
        st.info("⏳ Task is pending...")
        st.info("🔄 Auto-refreshing in 5 seconds...")
        time.sleep(5)
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
                st.info("🔄 Auto-refreshing in 3 seconds...")
                time.sleep(3)
                st.rerun()
            else:
                st.success("✅ Docking completed!")
        else:
            st.warning("⚠️ Progress data format unexpected")
            st.write(f"Raw progress data: {progress_data}")
    elif task.state == 'SUCCESS':
        st.success("✅ Docking completed successfully!")
        
        # Display results
        results = task.result
        if isinstance(results, dict):
            st.markdown("### 📊 Docking Results")
            
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Total Poses", results.get('total_docking_poses', 0))
            with col2:
                st.metric("Unique Ligands", results.get('unique_ligands', 0))
            with col3:
                st.metric("Unique Receptors", results.get('unique_receptors', 0))
            with col4:
                st.metric("Best Affinity", f"{results.get('best_affinity', 0):.2f} kcal/mol")
            
            # Load and display results
            results_file = results.get('docking_results_file')
            if results_file and os.path.exists(results_file):
                df_results = pd.read_csv(results_file)
                
                # Filter best poses per ligand-receptor pair
                df_best = df_results.loc[df_results.groupby(['ligand', 'receptor'])['affinity (kcal/mol)'].idxmin()]
                
                st.markdown("#### 🏆 Best Docking Poses")
                st.dataframe(
                    df_best[['ligand', 'receptor', 'affinity (kcal/mol)', 'rmsd l.b.', 'rmsd u.b.']].sort_values('affinity (kcal/mol)'),
                    use_container_width=True
                )
                
                # Affinity distribution plot
                fig = px.histogram(
                    df_results, 
                    x='affinity (kcal/mol)',
                    title='Distribution of Docking Affinities',
                    nbins=20
                )
                fig.update_layout(
                    xaxis_title="Affinity (kcal/mol)",
                    yaxis_title="Number of Poses"
                )
                st.plotly_chart(fig, use_container_width=True)
                
                # Download results
                st.markdown("#### 💾 Download Results")
                
                # Create ZIP with results
                zip_path = os.path.join(results.get('docking_output_dir'), 'docking_results.zip')
                with zipfile.ZipFile(zip_path, 'w') as zipf:
                    for root, dirs, files in os.walk(results.get('docking_output_dir')):
                        for file in files:
                            if file.endswith(('.csv', '.sdf', '.pdbqt')):
                                file_path = os.path.join(root, file)
                                zipf.write(file_path, os.path.relpath(file_path, results.get('docking_output_dir')))
                
                with open(zip_path, 'rb') as f:
                    st.download_button(
                        label="📥 Download All Results (ZIP)",
                        data=f.read(),
                        file_name=f"docking_results_{st.session_state.docking_job_id}.zip",
                        mime="application/zip"
                    )
                
    elif task.state == 'FAILURE':
        st.error("❌ Docking failed!")
        if isinstance(task.info, dict):
            error_msg = task.info.get('exc_message', 'Unknown error')
        else:
            error_msg = str(task.info) if task.info else 'Unknown error'
        st.error(f"Error: {error_msg}")

# Footer
st.markdown("---")
st.markdown("""
<div style='text-align: center; color: #666;'>
    <p>🔬 Molecular Docking powered by SMINA | Part of the PocketHunter Suite</p>
</div>
""", unsafe_allow_html=True) 