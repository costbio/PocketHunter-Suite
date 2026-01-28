import streamlit as st
import os
import pandas as pd
from datetime import datetime
import time
import json
import uuid
from tasks import run_extract_to_pdb_task
from celery_app import celery_app
from config import Config
from security import handle_file_upload_secure, SecurityError
from logging_config import setup_logging

# Use Config for directories
UPLOAD_DIR = str(Config.UPLOAD_DIR)
RESULTS_DIR = str(Config.RESULTS_DIR)

# Setup logging
logger = setup_logging(__name__)

# Session state initialization
if 'extract_job_id' not in st.session_state:
    st.session_state.extract_job_id = None
if 'extract_task_id' not in st.session_state:
    st.session_state.extract_task_id = None
if 'extract_status' not in st.session_state:
    st.session_state.extract_status = 'idle'

# Helper functions
# Note: Using secure upload handler from security.py instead of local function

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
    <h2>📁 Step 1: Extract Frames to PDB</h2>
    <p>Extract frames from molecular dynamics trajectory and convert to PDB format for pocket detection</p>
</div>
""", unsafe_allow_html=True)

# Display current job ID if exists
if st.session_state.extract_job_id:
    st.markdown(f"""
    <div class="job-id-display">
        🔑 Current Job ID: {st.session_state.extract_job_id}
    </div>
    """, unsafe_allow_html=True)
    st.info("💡 Copy this Job ID to use in Step 2: Detect Pockets")

# Force refresh section
st.markdown("### 🔄 Task Management")
col1, col2 = st.columns(2)

with col1:
    if st.button("🔄 Force Refresh", key="force_refresh_extract"):
        # Clear all task-related session state
        if 'extract_task_id' in st.session_state:
            del st.session_state.extract_task_id
        if 'extract_status' in st.session_state:
            del st.session_state.extract_status
        st.success("Task state cleared! You can now start a new extraction.")
        st.rerun()

with col2:
    if st.button("🗑️ Clear All Data", key="clear_all_extract"):
        # Clear all session state
        for key in list(st.session_state.keys()):
            if key.startswith('extract_'):
                del st.session_state[key]
        st.success("All extraction data cleared!")
        st.rerun()

# File upload section
st.markdown("### 📁 Input Files")

col1, col2 = st.columns(2)

with col1:
    st.markdown("#### Trajectory File")
    xtc_file = st.file_uploader(
        "Upload XTC Trajectory File (.xtc)",
        type=['xtc'],
        key="extract_xtc",
        help="Upload your molecular dynamics trajectory file"
    )

with col2:
    st.markdown("#### Topology File")
    topology_file = st.file_uploader(
        "Upload Topology File (.pdb, .gro)",
        type=['pdb', 'gro'],
        key="extract_topology",
        help="Upload your topology file (PDB or GRO format)"
    )

# Parameters
st.markdown("### ⚙️ Extraction Parameters")

col1, col2 = st.columns(2)

with col1:
    stride = st.number_input(
        "Frame Extraction Stride",
        min_value=1,
        value=10,
        key="extract_stride",
        help="Extract every Nth frame from trajectory"
    )

with col2:
    num_threads = st.number_input(
        "Number of Threads",
        min_value=1,
        value=4,
        key="extract_threads",
        help="Number of CPU threads to use"
    )

# Run button
st.markdown("---")
if st.button("🚀 Start Frame Extraction", type="primary", use_container_width=True):
    if not xtc_file or not topology_file:
        st.error("Please upload both trajectory and topology files.")
    else:
        # Generate unique job ID
        job_id = f"extract_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        st.session_state.extract_job_id = job_id

        # Handle file uploads with security validation
        try:
            xtc_path = str(handle_file_upload_secure(xtc_file, job_id, "trajectory_"))
            topology_path = str(handle_file_upload_secure(topology_file, job_id, "topology_"))
            logger.info(f"Files uploaded successfully for job {job_id}")
        except SecurityError as e:
            st.error(f"❌ File upload failed: {e}")
            logger.error(f"Security error during upload for job {job_id}: {e}")
            st.stop()
        except Exception as e:
            st.error(f"❌ Unexpected error during file upload: {e}")
            logger.error(f"Upload error for job {job_id}: {e}", exc_info=True)
            st.stop()

        if xtc_path and topology_path:
            # Update status
            update_job_status(job_id, 'submitted', 'Initializing frame extraction')
            st.session_state.extract_status = 'running'
            
            # Start the extraction
            with st.spinner("Starting frame extraction..."):
                try:
                    task = run_extract_to_pdb_task.delay(
                        xtc_file_path=xtc_path,
                        topology_file_path=topology_path,
                        stride=stride,
                        num_threads=num_threads,
                        job_id=job_id
                    )
                    
                    # Ensure task_id is properly stored
                    if task and task.id:
                        st.session_state.extract_task_id = task.id
                        update_job_status(job_id, 'running', 'Frame extraction started', task_id=task.id)
                        st.success(f"Frame extraction started! Job ID: {job_id}")
                        st.info("Monitor progress below or in the Task Monitor tab.")
                    else:
                        st.error("Failed to start task - no task ID received")
                        st.session_state.extract_status = 'failed'
                        
                except Exception as e:
                    st.error(f"Error starting task: {str(e)}")
                    st.session_state.extract_status = 'failed'

# Status monitoring - show for any active task or completed task
if st.session_state.extract_task_id or st.session_state.extract_status == 'completed':
    st.markdown("### 📊 Extraction Status")
    
    # Check if we have a valid task_id
    if st.session_state.extract_task_id:
        try:
            task = celery_app.AsyncResult(st.session_state.extract_task_id)
        except Exception as e:
            st.error(f"Error checking task status: {str(e)}")
            st.error("This might be due to an old or invalid task ID. Try refreshing the page or starting a new task.")
            
            # Add button to clear the task
            if st.button("🔄 Clear Task and Start Fresh", key="clear_extract_task"):
                st.session_state.extract_task_id = None
                st.session_state.extract_status = 'idle'
                st.success("Task cleared! You can now start a new extraction.")
                st.rerun()
            
            st.stop()
    else:
        # No task_id but status is completed - show completion status
        st.markdown('<div class="status-success">✅ Frame extraction completed successfully!</div>', unsafe_allow_html=True)
        
        # Display results from output files
        if st.session_state.extract_job_id:
            output_dir = os.path.join(RESULTS_DIR, st.session_state.extract_job_id, "pdbs")
            if os.path.exists(output_dir):
                pdb_files = [f for f in os.listdir(output_dir) if f.endswith('.pdb')]
                if pdb_files:
                    st.markdown("### 📈 Results")
                    
                    # Display summary metrics
                    col1, col2, col3 = st.columns(3)
                    
                    with col1:
                        st.metric("Frames Extracted", len(pdb_files))
                    
                    with col2:
                        st.metric("Output Directory", os.path.basename(output_dir))
                    
                    with col3:
                        st.metric("Files Found", len(pdb_files))
                    
                    st.success(f"✅ Frame extraction complete! Found {len(pdb_files)} PDB files. Use this Job ID in Step 2: Detect Pockets")
                    
                    # Show job ID prominently
                    st.markdown(f"""
                    <div class="job-id-display">
                        🔑 Job ID: {st.session_state.extract_job_id}
                    </div>
                    """, unsafe_allow_html=True)
                    st.info("💡 Copy this Job ID to use in Step 2: Detect Pockets")
    
    # BULLETPROOF: ALWAYS show progress bar if there's any task activity - NEVER let it disappear!
    show_progress = False
    progress_info = {}
    current_step = "Processing..."
    progress_percent = 0
    status = "Running..."
    task_state = "UNKNOWN"
    
    # Check if we have a task ID
    if st.session_state.extract_task_id:
        show_progress = True
        task = celery_app.AsyncResult(st.session_state.extract_task_id)
        progress_info = task.info or {}
        current_step = progress_info.get('current_step', 'Processing...')
        progress_percent = progress_info.get('progress', 0)
        status = progress_info.get('status', 'Running...')
        task_state = task.state
    
    # Check if status is running (fallback)
    elif st.session_state.extract_status == 'running':
        show_progress = True
        progress_percent = 50  # Default to 50% if we don't know
        status = "Running..."
        task_state = "PROGRESS"
    
    # Check if we have a job ID and status is completed (show results with progress bar)
    elif st.session_state.extract_job_id and st.session_state.extract_status == 'completed':
        show_progress = True
        progress_percent = 100
        current_step = "Frame extraction completed successfully!"
        status = "Completed"
        task_state = "SUCCESS"
    
    # If we should show progress, ALWAYS show it
    if show_progress:
        # Status indicator based on task state
        if task_state == 'PENDING':
            st.markdown('<div class="status-info">⏳ Frame extraction is queued and waiting to start...</div>', unsafe_allow_html=True)
            # Set initial progress for pending tasks
            progress_percent = 0
            current_step = "Waiting to start..."
            status = "Queued..."
        elif task_state == 'PROGRESS':
            st.markdown(f'<div class="status-info">🔄 Frame extraction is running: {current_step}</div>', unsafe_allow_html=True)
        elif task_state == 'SUCCESS':
            st.markdown('<div class="status-success">✅ Frame extraction completed successfully!</div>', unsafe_allow_html=True)
            # Keep progress at 100% for completed tasks
            progress_percent = 100
            current_step = "Frame extraction completed successfully!"
            status = "Completed"
        elif task_state == 'FAILURE':
            st.markdown('<div class="status-error">❌ Frame extraction failed!</div>', unsafe_allow_html=True)
            # Keep progress visible even for failed tasks
            current_step = "Task failed"
            status = "Failed"
        else:
            st.markdown(f'<div class="status-info">🔄 Frame extraction status: {task_state}</div>', unsafe_allow_html=True)
        
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
        if st.session_state.extract_task_id and task.ready() and task.successful():
            st.session_state.extract_status = 'completed'
            st.session_state.cached_job_ids['extract'] = st.session_state.extract_job_id

            # Update job status file to 'completed'
            result = task.result
            if result:
                update_job_status(
                    st.session_state.extract_job_id,
                    'completed',
                    'Frame extraction completed',
                    result_info={
                        'frames_extracted': result.get('frames_extracted', 0),
                        'output_files': result.get('output_files', [])
                    }
                )

            # Display results
            if result:
                st.markdown("### 📈 Results")
                
                # Display summary metrics
                col1, col2, col3 = st.columns(3)
                
                with col1:
                    st.metric("Frames Extracted", result.get('frames_extracted', 'N/A'))
                
                with col2:
                    st.metric("Output Directory", os.path.basename(result.get('pdb_output_dir', 'N/A')))
                
                with col3:
                    st.metric("Processing Time", f"{result.get('processing_time', 0):.1f}s")
                
                st.success("✅ Frame extraction complete! Use this Job ID in Step 2: Detect Pockets")
                
                # Show job ID prominently
                st.markdown(f"""
                <div class="job-id-display">
                    🔑 Job ID: {st.session_state.extract_job_id}
                </div>
                """, unsafe_allow_html=True)
                st.info("💡 Copy this Job ID to use in Step 2: Detect Pockets")
        
        # Add action buttons - always visible when there's a task
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("❌ Cancel Task", key="cancel_extract_task"):
                try:
                    if st.session_state.extract_task_id:
                        task = celery_app.AsyncResult(st.session_state.extract_task_id)
                        task.revoke(terminate=True)
                    st.session_state.extract_task_id = None
                    st.session_state.extract_status = 'cancelled'
                    st.success("Task cancelled successfully!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error cancelling task: {str(e)}")
        
        with col2:
            if st.button("🔍 Check Task Status", key="check_task_status"):
                if st.session_state.extract_task_id:
                    task = celery_app.AsyncResult(st.session_state.extract_task_id)
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
            if st.session_state.extract_task_id:
                task = celery_app.AsyncResult(st.session_state.extract_task_id)
                st.write(f"**Task State:** {task.state}")
                st.write(f"**Task ID:** {st.session_state.extract_task_id}")
                st.write(f"**Task Ready:** {task.ready()}")
                if task.ready():
                    st.write(f"**Task Result:** {task.result}")
            else:
                st.write("**No active task ID**")
                st.write(f"**Session Status:** {st.session_state.extract_status}")
                st.write(f"**Job ID:** {st.session_state.extract_job_id}")

# Handle completed tasks that don't have task_id anymore
elif st.session_state.extract_status == 'completed' and st.session_state.extract_job_id:
    # Show progress bar for completed tasks too - NEVER let it disappear!
    st.markdown('<div class="status-success">✅ Frame extraction completed successfully!</div>', unsafe_allow_html=True)
    
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
        st.metric("Current Step", "Frame extraction completed successfully!")
    
    # Display results from output files
    output_dir = os.path.join(RESULTS_DIR, st.session_state.extract_job_id, "pdbs")
    if os.path.exists(output_dir):
        pdb_files = [f for f in os.listdir(output_dir) if f.endswith('.pdb')]
        if pdb_files:
            st.markdown("### 📈 Results")
            
            # Display summary metrics
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.metric("Frames Extracted", len(pdb_files))
            
            with col2:
                st.metric("Output Directory", os.path.basename(output_dir))
            
            with col3:
                st.metric("Files Found", len(pdb_files))
            
            st.success(f"✅ Frame extraction complete! Found {len(pdb_files)} PDB files. Use this Job ID in Step 2: Detect Pockets")
            
            # Show job ID prominently
            st.markdown(f"""
            <div class="job-id-display">
                🔑 Job ID: {st.session_state.extract_job_id}
            </div>
            """, unsafe_allow_html=True)
            st.info("💡 Copy this Job ID to use in Step 2: Detect Pockets")

# Debug section to understand what's happening
with st.expander("🐛 Debug Session State"):
    st.write("**Session State Debug Info:**")
    st.write(f"extract_task_id: {st.session_state.get('extract_task_id', 'None')}")
    st.write(f"extract_status: {st.session_state.get('extract_status', 'None')}")
    st.write(f"extract_job_id: {st.session_state.get('extract_job_id', 'None')}")
    st.write(f"cached_job_ids: {st.session_state.get('cached_job_ids', {})}")
    
    # Check if we have any task activity
    has_task_id = bool(st.session_state.get('extract_task_id'))
    has_running_status = st.session_state.get('extract_status') == 'running'
    has_completed_status = st.session_state.get('extract_status') == 'completed'
    has_job_id = bool(st.session_state.get('extract_job_id'))
    
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
if st.session_state.extract_status == 'running':
    # Check if task is ready to avoid unnecessary refreshes
    if st.session_state.extract_task_id:
        try:
            task = celery_app.AsyncResult(st.session_state.extract_task_id)
            
            # Check if task is completed
            if task.ready():
                # Task is done, update status and refresh immediately
                st.session_state.extract_status = 'completed'
                st.rerun()
            else:
                # Task still running, poll at reasonable interval to avoid browser freeze
                time.sleep(3)  # Reasonable polling interval
                st.rerun()
                
        except Exception as e:
            # Fallback: Check if task is actually completed by looking at output files
            if st.session_state.extract_job_id:
                output_dir = os.path.join(RESULTS_DIR, st.session_state.extract_job_id, "pdbs")
                if os.path.exists(output_dir):
                    pdb_files = [f for f in os.listdir(output_dir) if f.endswith('.pdb')]
                    if pdb_files:
                        # Task completed but state wasn't updated
                        st.session_state.extract_status = 'completed'
                        st.session_state.cached_job_ids['extract'] = st.session_state.extract_job_id
                        # Don't clear task_id so status section stays visible
                        st.rerun()
            
            # Fallback refresh - reasonable interval
            time.sleep(3)
            st.rerun()
    else:
        # No task ID, check for completion via output files
        if st.session_state.extract_job_id:
            output_dir = os.path.join(RESULTS_DIR, st.session_state.extract_job_id, "pdbs")
            if os.path.exists(output_dir):
                pdb_files = [f for f in os.listdir(output_dir) if f.endswith('.pdb')]
                if pdb_files:
                    # Task completed but no task_id - update status
                    st.session_state.extract_status = 'completed'
                    st.session_state.cached_job_ids['extract'] = st.session_state.extract_job_id
                    st.rerun()
        
        # No task ID, poll at reasonable interval
        time.sleep(3)
        st.rerun() 