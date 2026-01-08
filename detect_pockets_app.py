import streamlit as st
import os
import pandas as pd
from datetime import datetime
import time
import json
import uuid
import zipfile
from tasks import run_detect_pockets_task
from celery_app import celery_app
from config import Config
from security import handle_file_upload_secure, SecurityError, FileValidator
from logging_config import setup_logging

# Use Config for directories
UPLOAD_DIR = str(Config.UPLOAD_DIR)
RESULTS_DIR = str(Config.RESULTS_DIR)

# Setup logging
logger = setup_logging(__name__)

# Session state initialization
if 'detect_job_id' not in st.session_state:
    st.session_state.detect_job_id = None
if 'detect_task_id' not in st.session_state:
    st.session_state.detect_task_id = None
if 'detect_status' not in st.session_state:
    st.session_state.detect_status = 'idle'

# Helper functions
# Note: Using secure upload handler from security.py

def extract_zip_to_directory(zip_path, extract_dir):
    """Extract ZIP file to directory and return list of PDB files (WITH SECURITY VALIDATION)"""
    from pathlib import Path
    # Validate ZIP before extraction
    try:
        FileValidator.validate_zip_file(Path(zip_path))
        logger.info(f"ZIP file validated: {zip_path}")
    except SecurityError as e:
        logger.error(f"ZIP validation failed: {e}")
        raise
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(extract_dir)
    
    # Find all PDB files
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

# Main UI
st.markdown("""
<div class="metric-card">
    <h2>🔍 Step 2: Detect Pockets</h2>
    <p>Detect potential ligand-binding pockets from PDB structure files</p>
</div>
""", unsafe_allow_html=True)

# Display current job ID if exists
if st.session_state.detect_job_id:
    st.markdown(f"""
    <div class="job-id-display">
        🔑 Current Job ID: {st.session_state.detect_job_id}
    </div>
    """, unsafe_allow_html=True)
    st.info("💡 Copy this Job ID to use in Step 3: Cluster Pockets")

# Input options
st.markdown("### 📁 Input Options")

# Option 1: Use previous step results
st.markdown("#### Option 1: Use Previous Step Results")
# Use cached job ID if available
cached_extract_id = st.session_state.cached_job_ids.get('extract', '')
extract_job_id = st.text_input(
    "Enter Job ID from Step 1 (Extract Frames):",
    value=cached_extract_id,
    key="detect_extract_job_id",
    help="Enter the Job ID from the previous frame extraction step"
)

# Option 2: Upload PDB files
st.markdown("#### Option 2: Upload PDB Files")
pdb_zip = st.file_uploader(
    "Upload PDB Files (as ZIP archive)",
    type=['zip'],
    key="detect_pdb_zip",
    help="Upload a ZIP file containing PDB structure files"
)

# Option 3: Manual path
st.markdown("#### Option 3: Manual Directory Path")
manual_pdb_path = st.text_input(
    "Enter path to PDB directory on server:",
    key="detect_manual_path",
    help="Enter the full path to a directory containing PDB files"
)

# Parameters
st.markdown("### ⚙️ Detection Parameters")

num_threads = st.number_input(
    "Number of Threads",
    min_value=1,
    value=4,
    key="detect_threads",
    help="Number of CPU threads to use for pocket detection"
)

# Run button
st.markdown("---")
if st.button("🚀 Start Pocket Detection", type="primary", use_container_width=True):
    # Determine input source
    input_pdb_path = None
    input_source = None
    
    if extract_job_id:
        # Use results from previous step
        extract_output_dir = os.path.join(RESULTS_DIR, extract_job_id, "pdbs")
        if os.path.exists(extract_output_dir) and os.listdir(extract_output_dir):
            input_pdb_path = extract_output_dir
            input_source = f"Step 1 results (Job ID: {extract_job_id})"
        else:
            st.error(f"PDB files not found for Job ID: {extract_job_id}")
            st.stop()
    
    elif pdb_zip:
        # Extract uploaded ZIP
        job_id = f"detect_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        extract_dir = os.path.join(UPLOAD_DIR, job_id, "extracted_pdbs")
        os.makedirs(extract_dir, exist_ok=True)

        # Upload and validate ZIP file
        try:
            zip_path = handle_file_upload_secure(pdb_zip, job_id, "pdbs_")
            logger.info(f"ZIP file uploaded for job {job_id}")
        except SecurityError as e:
            st.error(f"❌ File upload failed: {e}")
            logger.error(f"Security error during ZIP upload for job {job_id}: {e}")
            st.stop()
        if zip_path:
            pdb_files = extract_zip_to_directory(zip_path, extract_dir)
            if pdb_files:
                input_pdb_path = extract_dir
                input_source = f"Uploaded ZIP ({len(pdb_files)} PDB files)"
            else:
                st.error("No PDB files found in uploaded ZIP")
                st.stop()
    
    elif manual_pdb_path and os.path.exists(manual_pdb_path):
        input_pdb_path = manual_pdb_path
        input_source = f"Manual path: {manual_pdb_path}"
    
    else:
        st.error("Please provide input PDB files using one of the three options above.")
        st.stop()
    
    if input_pdb_path:
        # Generate unique job ID
        job_id = f"detect_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        st.session_state.detect_job_id = job_id
        
        # Update status
        update_job_status(job_id, 'submitted', 'Initializing pocket detection')
        st.session_state.detect_status = 'running'
        
        # Start the detection
        with st.spinner("Starting pocket detection..."):
            task = run_detect_pockets_task.delay(
                input_pdb_path_abs=os.path.abspath(input_pdb_path),
                job_id=job_id,
                numthreads=num_threads
            )
            st.session_state.detect_task_id = task.id
            update_job_status(job_id, 'running', 'Pocket detection started', task_id=task.id)
            
        st.success(f"Pocket detection started! Job ID: {job_id}")
        st.info(f"Input source: {input_source}")
        st.info("Monitor progress below or in the Task Monitor tab.")

# Status monitoring
if st.session_state.detect_task_id:
    st.markdown("### 📊 Detection Status")
    
    try:
        task = celery_app.AsyncResult(st.session_state.detect_task_id)
    except Exception as e:
        st.error(f"Error checking task status: {str(e)}")
        st.session_state.detect_task_id = None
        st.session_state.detect_status = 'failed'
        st.stop()
    
    # BULLETPROOF: ALWAYS show progress bar if there's any task activity - NEVER let it disappear!
    show_progress = False
    progress_info = {}
    current_step = "Processing..."
    progress_percent = 0
    status = "Running..."
    task_state = "UNKNOWN"
    
    # Check if we have a task ID
    if st.session_state.detect_task_id:
        show_progress = True
        task = celery_app.AsyncResult(st.session_state.detect_task_id)
        progress_info = task.info or {}
        current_step = progress_info.get('current_step', 'Processing...')
        progress_percent = progress_info.get('progress', 0)
        status = progress_info.get('status', 'Running...')
        task_state = task.state
    
    # Check if status is running (fallback)
    elif st.session_state.detect_status == 'running':
        show_progress = True
        progress_percent = 50  # Default to 50% if we don't know
        status = "Running..."
        task_state = "PROGRESS"
    
    # Check if we have a job ID and status is completed (show results with progress bar)
    elif st.session_state.detect_job_id and st.session_state.detect_status == 'completed':
        show_progress = True
        progress_percent = 100
        current_step = "Pocket detection completed successfully!"
        status = "Completed"
        task_state = "SUCCESS"
    
    # If we should show progress, ALWAYS show it
    if show_progress:
        # Status indicator based on task state
        if task_state == 'PENDING':
            st.markdown('<div class="status-info">⏳ Pocket detection is queued and waiting to start...</div>', unsafe_allow_html=True)
            # Set initial progress for pending tasks
            progress_percent = 0
            current_step = "Waiting to start..."
            status = "Queued..."
        elif task_state == 'PROGRESS':
            st.markdown(f'<div class="status-info">🔄 Pocket detection is running: {current_step}</div>', unsafe_allow_html=True)
        elif task_state == 'SUCCESS':
            st.markdown('<div class="status-success">✅ Pocket detection completed successfully!</div>', unsafe_allow_html=True)
            # Keep progress at 100% for completed tasks
            progress_percent = 100
            current_step = "Pocket detection completed successfully!"
            status = "Completed"
        elif task_state == 'FAILURE':
            st.markdown('<div class="status-error">❌ Pocket detection failed!</div>', unsafe_allow_html=True)
            # Keep progress visible even for failed tasks
            current_step = "Task failed"
            status = "Failed"
        else:
            st.markdown(f'<div class="status-info">🔄 Pocket detection status: {task_state}</div>', unsafe_allow_html=True)
        
        # ALWAYS show the progress bar - NEVER disappears!
        st.markdown("### 📊 Progress")
        progress_bar = st.progress(progress_percent / 100)
        
        # Progress details in columns - always visible
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.metric("Progress", f"{progress_percent:.1f}%")
        
        with col2:
            if 'elapsed' in progress_info:
                elapsed = progress_info['elapsed']
                st.metric("Elapsed Time", f"{elapsed:.1f}s")
            else:
                st.metric("Status", status)
        
        with col3:
            st.metric("Current Step", current_step[:20] + "..." if len(current_step) > 20 else current_step)
        
        # Show detailed status - always visible
        st.write(f"**Status:** {status}")
        
        # Show warning if task is taking too long (only for running tasks)
        if task_state == 'PROGRESS' and progress_percent < 50 and 'elapsed' in progress_info and progress_info['elapsed'] > 300:  # 5 minutes
            st.warning("⚠️ Task is taking longer than expected. This might indicate an issue with the input files or system resources.")
        
        # Check if task is actually completed and show results
        if st.session_state.detect_task_id and task.ready() and task.successful():
            st.session_state.detect_status = 'completed'
            st.session_state.cached_job_ids['detect'] = st.session_state.detect_job_id
            
            # Display results
            result = task.result
            if result:
                st.markdown("### 📈 Results")
                
                # Display summary metrics
                col1, col2, col3 = st.columns(3)
                
                with col1:
                    st.metric("Pockets Detected", result.get('pockets_detected', 'N/A'))
                
                with col2:
                    st.metric("Output Directory", os.path.basename(result.get('pockets_output_dir', 'N/A')))
                
                with col3:
                    st.metric("Processing Time", f"{result.get('processing_time', 0):.1f}s")
                
                st.success("✅ Pocket detection complete! Use this Job ID in Step 3: Cluster Pockets")
                
                # Show job ID prominently
                st.markdown(f"""
                <div class="job-id-display">
                    🔑 Job ID: {st.session_state.detect_job_id}
                </div>
                """, unsafe_allow_html=True)
                st.info("💡 Copy this Job ID to use in Step 3: Cluster Pockets")
        
        # Add action buttons - always visible when there's a task
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("❌ Cancel Task", key="cancel_detect_task"):
                try:
                    if st.session_state.detect_task_id:
                        task = celery_app.AsyncResult(st.session_state.detect_task_id)
                        task.revoke(terminate=True)
                    st.session_state.detect_task_id = None
                    st.session_state.detect_status = 'cancelled'
                    st.success("Task cancelled successfully!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error cancelling task: {str(e)}")
        
        with col2:
            if st.button("🔍 Check Task Status", key="check_task_status"):
                if st.session_state.detect_task_id:
                    task = celery_app.AsyncResult(st.session_state.detect_task_id)
                    st.write(f"**Current Task State:** {task.state}")
                    st.write(f"**Task Ready:** {task.ready()}")
                    if task.ready():
                        st.write(f"**Task Result:** {task.result}")
                else:
                    st.write("**No active task ID**")
                st.rerun()
        
        # Show debug info in an expander - always visible
        with st.expander("🔍 Debug Information"):
            st.json(progress_info)
            if st.session_state.detect_task_id:
                task = celery_app.AsyncResult(st.session_state.detect_task_id)
                st.write(f"**Task State:** {task.state}")
                st.write(f"**Task ID:** {st.session_state.detect_task_id}")
                st.write(f"**Task Ready:** {task.ready()}")
                if task.ready():
                    st.write(f"**Task Result:** {task.result}")
            else:
                st.write("**No active task ID**")
                st.write(f"**Session Status:** {st.session_state.detect_status}")
                st.write(f"**Job ID:** {st.session_state.detect_job_id}")

# Handle completed tasks that don't have task_id anymore
elif st.session_state.detect_status == 'completed' and st.session_state.detect_job_id:
    # Show progress bar for completed tasks too - NEVER let it disappear!
    st.markdown('<div class="status-success">✅ Pocket detection completed successfully!</div>', unsafe_allow_html=True)
    
    # Show progress bar at 100% for completed tasks
    st.markdown("### 📊 Progress")
    progress_bar = st.progress(1.0)  # 100%
    
    # Progress details in columns - always visible
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric("Progress", "100.0%")
    
    with col2:
        st.metric("Status", "Completed")
    
    with col3:
        st.metric("Current Step", "Pocket detection completed successfully!")
    
    # Display results from output files
    output_dir = os.path.join(RESULTS_DIR, st.session_state.detect_job_id, "pockets")
    if os.path.exists(output_dir):
        pockets_csv = os.path.join(output_dir, "pockets.csv")
        if os.path.exists(pockets_csv):
            st.markdown("### 📈 Results")
            
            # Display summary metrics
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.metric("Output Directory", os.path.basename(output_dir))
            
            with col2:
                st.metric("Pockets CSV", "Found")
            
            with col3:
                st.metric("Status", "Completed")
            
            st.success("✅ Pocket detection complete! Use this Job ID in Step 3: Cluster Pockets")
            
            # Show job ID prominently
            st.markdown(f"""
            <div class="job-id-display">
                🔑 Job ID: {st.session_state.detect_job_id}
            </div>
            """, unsafe_allow_html=True)
            st.info("💡 Copy this Job ID to use in Step 3: Cluster Pockets")

# Debug section to understand what's happening
with st.expander("🐛 Debug Session State"):
    st.write("**Session State Debug Info:**")
    st.write(f"detect_task_id: {st.session_state.get('detect_task_id', 'None')}")
    st.write(f"detect_status: {st.session_state.get('detect_status', 'None')}")
    st.write(f"detect_job_id: {st.session_state.get('detect_job_id', 'None')}")
    st.write(f"cached_job_ids: {st.session_state.get('cached_job_ids', {})}")
    
    # Check if we have any task activity
    has_task_id = bool(st.session_state.get('detect_task_id'))
    has_running_status = st.session_state.get('detect_status') == 'running'
    has_completed_status = st.session_state.get('detect_status') == 'completed'
    has_job_id = bool(st.session_state.get('detect_job_id'))
    
    st.write("**Progress Bar Logic:**")
    st.write(f"Has task ID: {has_task_id}")
    st.write(f"Has running status: {has_running_status}")
    st.write(f"Has completed status: {has_completed_status}")
    st.write(f"Has job ID: {has_job_id}")
    
    # Show what condition would trigger progress bar
    condition1 = has_task_id
    condition2 = has_running_status
    condition3 = has_job_id and has_completed_status
    
    st.write("**Progress Bar Conditions:**")
    st.write(f"Condition 1 (task_id): {condition1}")
    st.write(f"Condition 2 (running): {condition2}")
    st.write(f"Condition 3 (completed): {condition3}")
    st.write(f"Should show progress: {condition1 or condition2 or condition3}")

# Auto-refresh with completion check - more responsive for quick tasks
if st.session_state.detect_status == 'running':
    # Check if task is ready to avoid unnecessary refreshes
    if st.session_state.detect_task_id:
        try:
            task = celery_app.AsyncResult(st.session_state.detect_task_id)
            
            # Check if task is completed
            if task.ready():
                # Task is done, update status and refresh immediately
                st.session_state.detect_status = 'completed'
                st.rerun()
            else:
                # Task still running, check for completion more aggressively
                # For quick tasks, check more frequently
                time.sleep(0.2)  # Very responsive for quick tasks
                st.rerun()
                
        except Exception as e:
            # Fallback: Check if task is actually completed by looking at output files
            if st.session_state.detect_job_id:
                output_dir = os.path.join(RESULTS_DIR, st.session_state.detect_job_id, "pockets")
                if os.path.exists(output_dir):
                    pockets_csv = os.path.join(output_dir, "pockets.csv")
                    if os.path.exists(pockets_csv):
                        # Task completed but state wasn't updated
                        st.session_state.detect_status = 'completed'
                        st.session_state.cached_job_ids['detect'] = st.session_state.detect_job_id
                        # Don't clear task_id so status section stays visible
                        st.rerun()
            
            # Fallback refresh - very quick for error recovery
            time.sleep(0.5)
            st.rerun()
    else:
        # No task ID, check for completion via output files
        if st.session_state.detect_job_id:
            output_dir = os.path.join(RESULTS_DIR, st.session_state.detect_job_id, "pockets")
            if os.path.exists(output_dir):
                pockets_csv = os.path.join(output_dir, "pockets.csv")
                if os.path.exists(pockets_csv):
                    # Task completed but no task_id - update status
                    st.session_state.detect_status = 'completed'
                    st.session_state.cached_job_ids['detect'] = st.session_state.detect_job_id
                    st.rerun()
        
        # No task ID, refresh less frequently
        time.sleep(1)
        st.rerun() 