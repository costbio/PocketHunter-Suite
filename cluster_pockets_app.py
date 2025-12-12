import streamlit as st
import os
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import time
import json
import uuid
from tasks import run_cluster_pockets_task
from celery_app import celery_app

# Base directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
RESULTS_DIR = os.path.join(BASE_DIR, "results")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)

# Session state initialization
if 'cluster_job_id' not in st.session_state:
    st.session_state.cluster_job_id = None
if 'cluster_task_id' not in st.session_state:
    st.session_state.cluster_task_id = None
if 'cluster_status' not in st.session_state:
    st.session_state.cluster_status = 'idle'

# Restore session state from status file if we have a job_id but no task_id
if st.session_state.cluster_job_id and not st.session_state.cluster_task_id:
    status_file = os.path.join(RESULTS_DIR, f'{st.session_state.cluster_job_id}_status.json')
    if os.path.exists(status_file):
        try:
            with open(status_file, 'r') as f:
                status_data = json.load(f)
                if 'task_id' in status_data:
                    st.session_state.cluster_task_id = status_data['task_id']
                    st.session_state.cluster_status = status_data.get('status', 'running')
                    # If task is completed, update cached job IDs
                    if status_data.get('status') == 'completed':
                        if 'cached_job_ids' not in st.session_state:
                            st.session_state.cached_job_ids = {}
                        st.session_state.cached_job_ids['cluster'] = st.session_state.cluster_job_id
        except Exception as e:
            st.error(f"Error reading status file: {str(e)}")

# Auto-restore cluster job_id from cached job IDs if not set
if not st.session_state.cluster_job_id and 'cached_job_ids' in st.session_state:
    cached_cluster_id = st.session_state.cached_job_ids.get('cluster')
    if cached_cluster_id:
        st.session_state.cluster_job_id = cached_cluster_id
        # Also try to restore task_id from status file
        status_file = os.path.join(RESULTS_DIR, f'{cached_cluster_id}_status.json')
        if os.path.exists(status_file):
            try:
                with open(status_file, 'r') as f:
                    status_data = json.load(f)
                    if 'task_id' in status_data:
                        st.session_state.cluster_task_id = status_data['task_id']
                        st.session_state.cluster_status = status_data.get('status', 'running')
            except Exception as e:
                st.error(f"Error reading status file: {str(e)}")

# Auto-detect running cluster jobs if no job_id is set
if not st.session_state.cluster_job_id:

    # Look for the most recent cluster job that's still running or completed
    cluster_jobs = [f for f in os.listdir(RESULTS_DIR) if f.startswith('cluster_') and f.endswith('_status.json')]
    if cluster_jobs:
        # Sort by modification time (most recent first)
        cluster_jobs.sort(key=lambda x: os.path.getmtime(os.path.join(RESULTS_DIR, x)), reverse=True)
        latest_job_file = cluster_jobs[0]
        job_id = latest_job_file.replace('_status.json', '')
        
        # Check if this job is still running or completed
        status_file = os.path.join(RESULTS_DIR, latest_job_file)
        try:
            with open(status_file, 'r') as f:
                status_data = json.load(f)
                status = status_data.get('status', 'running')
                if status in ['running', 'completed']:
                    st.session_state.cluster_job_id = job_id
                    if 'task_id' in status_data:
                        st.session_state.cluster_task_id = status_data['task_id']
                    st.session_state.cluster_status = status
                    
                    # If completed, update cached job IDs
                    if status == 'completed':
                        if 'cached_job_ids' not in st.session_state:
                            st.session_state.cached_job_ids = {}
                        st.session_state.cached_job_ids['cluster'] = job_id

 
        except Exception as e:
            st.error(f"Error reading status file: {str(e)}")


# Helper functions
def handle_file_upload(uploaded_file, job_id, filename_prefix=""):
    if uploaded_file is not None:
        job_upload_dir = os.path.join(UPLOAD_DIR, job_id)
        os.makedirs(job_upload_dir, exist_ok=True)
        
        target_filename = filename_prefix + uploaded_file.name
        filepath = os.path.join(job_upload_dir, target_filename)
        
        with open(filepath, "wb") as f:
            f.write(uploaded_file.getbuffer())
        return filepath
    return None

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
    <h2>🎯 Step 3: Cluster Pockets</h2>
    <p>Cluster detected pockets to identify representative binding sites</p>
</div>
""", unsafe_allow_html=True)

# Display current job ID if exists
if st.session_state.cluster_job_id:
    st.markdown(f"""
    <div class="job-id-display">
        🔑 Current Job ID: {st.session_state.cluster_job_id}
    </div>
    """, unsafe_allow_html=True)
    st.info("💡 This is the final step in the pipeline")

# Input options
st.markdown("### 📁 Input Options")

# Option 1: Use previous step results
st.markdown("#### Option 1: Use Previous Step Results")
# Use cached job ID if available
cached_detect_id = st.session_state.cached_job_ids.get('detect', '')
detect_job_id = st.text_input(
    "Enter Job ID from Step 2 (Detect Pockets):",
    value=cached_detect_id,
    key="cluster_detect_job_id",
    help="Enter the Job ID from the previous pocket detection step"
)

# Option 2: Upload pockets.csv
st.markdown("#### Option 2: Upload pockets.csv File")
pockets_csv = st.file_uploader(
    "Upload pockets.csv File",
    type=['csv'],
    key="cluster_pockets_csv",
    help="Upload a pockets.csv file from pocket detection"
)

# Option 3: Manual path
st.markdown("#### Option 3: Manual Directory Path")
manual_pockets_dir = st.text_input(
    "Enter path to directory containing pockets.csv:",
    key="cluster_manual_path",
    help="Enter the full path to a directory containing pockets.csv"
)

# Parameters
st.markdown("### ⚙️ Clustering Parameters")

col1, col2 = st.columns(2)

with col1:
    min_prob = st.slider(
        "Min. Ligand-Binding Probability",
        min_value=0.0,
        max_value=1.0,
        value=0.5,
        step=0.05,
        key="cluster_min_prob",
        help="Minimum probability threshold for pocket clustering"
    )

with col2:
    clustering_method = st.selectbox(
        "Clustering Method",
        options=["dbscan", "hierarchical"],
        index=0,
        key="cluster_method",
        help="DBSCAN: Density-based clustering optimized for molecular pockets | Hierarchical: Tree-based clustering with distance threshold"
    )

# Advanced options - only show for DBSCAN
if clustering_method == "dbscan":
    st.markdown("#### Advanced Options")
    dbscan_hierarchical = st.checkbox(
        "Enable Hierarchical Refinement",
        value=True,
        key="cluster_dbscan_hierarchical",
        help="Apply hierarchical sub-clustering within each DBSCAN cluster for finer pocket grouping"
    )
else:
    # For hierarchical method, this option doesn't apply
    dbscan_hierarchical = False

# Run button
st.markdown("---")
if st.button("🚀 Start Pocket Clustering", type="primary", use_container_width=True):
    # Determine input source
    pockets_csv_path = None
    input_source = None
    
    if detect_job_id:
        # Use results from previous step
        detect_output_dir = os.path.join(RESULTS_DIR, detect_job_id, "pockets")
        potential_csv_path = os.path.join(detect_output_dir, "pockets.csv")
        if os.path.exists(potential_csv_path):
            pockets_csv_path = potential_csv_path
            input_source = f"Step 2 results (Job ID: {detect_job_id})"
        else:
            st.error(f"pockets.csv not found for Job ID: {detect_job_id}")
            st.stop()
    
    elif pockets_csv:
        # Upload pockets.csv
        job_id = f"cluster_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        csv_path = handle_file_upload(pockets_csv, job_id, "pockets_")
        if csv_path:
            pockets_csv_path = csv_path
            input_source = "Uploaded pockets.csv"
    
    elif manual_pockets_dir and os.path.exists(manual_pockets_dir):
        potential_csv_path = os.path.join(manual_pockets_dir, "pockets.csv")
        if os.path.exists(potential_csv_path):
            pockets_csv_path = potential_csv_path
            input_source = f"Manual path: {manual_pockets_dir}"
        else:
            st.error(f"pockets.csv not found in directory: {manual_pockets_dir}")
            st.stop()
    
    else:
        st.error("Please provide pockets.csv using one of the three options above.")
        st.stop()
    
    if pockets_csv_path:
        # Generate unique job ID
        job_id = f"cluster_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        st.session_state.cluster_job_id = job_id
        
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
            
        st.success(f"Pocket clustering started! Job ID: {job_id}")
        st.info(f"Input source: {input_source}")
        st.info("Monitor progress below or in the Task Monitor tab.")

# Status monitoring - Show progress for any running or completed task
show_progress = False
progress_info = {}
current_step = "Processing..."
progress_percent = 0
status = "Running..."
task_state = "UNKNOWN"





# Check if we have a task ID
task = None
if st.session_state.cluster_task_id:

    show_progress = True
    try:
        task = celery_app.AsyncResult(st.session_state.cluster_task_id)
        progress_info = task.info or {}
        current_step = progress_info.get('current_step', 'Processing...')
        progress_percent = progress_info.get('progress', 0)
        status = progress_info.get('status', 'Running...')
        task_state = task.state
        
        # If task is completed, override progress to 100%
        if task_state == 'SUCCESS':
            progress_percent = 100
            current_step = "Pocket clustering completed successfully!"
            status = "Completed"
        elif task_state == 'FAILURE':
            current_step = "Task failed"
            status = "Failed"
            
    except Exception as e:
        st.error(f"Error checking task status: {str(e)}")
        st.session_state.cluster_task_id = None
        st.session_state.cluster_status = 'failed'
        st.stop()

# Check if status is running (fallback)
elif st.session_state.cluster_status == 'running':

    show_progress = True
    progress_percent = 50  # Default to 50% if we don't know
    status = "Running..."
    task_state = "PROGRESS"

# Check if we have a job ID and status is completed (show results with progress bar)
elif st.session_state.cluster_job_id and st.session_state.cluster_status == 'completed':

    show_progress = True
    progress_percent = 100
    current_step = "Pocket clustering completed successfully!"
    status = "Completed"
    task_state = "SUCCESS"

# If we should show progress, ALWAYS show it

if show_progress:

    st.markdown("### 📊 Clustering Status")
    
    # Status indicator based on task state
    if task_state == 'PENDING':
        st.markdown('<div class="status-info">⏳ Pocket clustering is queued and waiting to start...</div>', unsafe_allow_html=True)
        # Set initial progress for pending tasks
        progress_percent = 0
        current_step = "Waiting to start..."
        status = "Queued..."
    elif task_state == 'PROGRESS':
        st.markdown(f'<div class="status-info">🔄 Pocket clustering is running: {current_step}</div>', unsafe_allow_html=True)
    elif task_state == 'SUCCESS':
        st.markdown('<div class="status-success">✅ Pocket clustering completed successfully!</div>', unsafe_allow_html=True)
        # Keep progress at 100% for completed tasks
        progress_percent = 100
        current_step = "Pocket clustering completed successfully!"
        status = "Completed"
    elif task_state == 'FAILURE':
        st.markdown('<div class="status-error">❌ Pocket clustering failed!</div>', unsafe_allow_html=True)
        # Keep progress visible even for failed tasks
        current_step = "Task failed"
        status = "Failed"
    else:
        st.markdown(f'<div class="status-info">🔄 Pocket clustering status: {task_state}</div>', unsafe_allow_html=True)
    
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
    
    # IMMEDIATE completion detection - check if task is ready and update status
if st.session_state.cluster_task_id and task and task.ready():
    if task.successful():
        st.session_state.cluster_status = 'completed'
        if 'cached_job_ids' not in st.session_state:
            st.session_state.cached_job_ids = {}
        st.session_state.cached_job_ids['cluster'] = st.session_state.cluster_job_id
        # Update the status file to reflect completion
        update_job_status(st.session_state.cluster_job_id, 'completed', 'Pocket clustering completed successfully')
        st.rerun()  # Immediately refresh to show results
    else:
        st.session_state.cluster_status = 'failed'
        st.error(f"Task failed: {task.result}")
        st.rerun()

# Also check status file for completion if task is not ready but status file says completed
elif st.session_state.cluster_job_id and st.session_state.cluster_status != 'completed':
    status_file = os.path.join(RESULTS_DIR, f'{st.session_state.cluster_job_id}_status.json')
    if os.path.exists(status_file):
        try:
            with open(status_file, 'r') as f:
                status_data = json.load(f)
                if status_data.get('status') == 'completed':
                    st.session_state.cluster_status = 'completed'
                    if 'cached_job_ids' not in st.session_state:
                        st.session_state.cached_job_ids = {}
                    st.session_state.cached_job_ids['cluster'] = st.session_state.cluster_job_id
                    st.rerun()  # Immediately refresh to show results
        except Exception as e:
            pass  # Silently ignore errors here
    
    # Check if task is actually completed and show results
    if st.session_state.cluster_task_id and task and task.ready() and task.successful():
        st.session_state.cluster_status = 'completed'
        st.session_state.cached_job_ids['cluster'] = st.session_state.cluster_job_id
        
        # Display results
        result = task.result
        if result:
            st.markdown("### 📈 Results")
            
            # Display summary metrics
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.metric("Clusters Found", result.get('clusters_found', 'N/A'))
            
            with col2:
                st.metric("Output Directory", os.path.basename(result.get('clusters_output_dir', 'N/A')))
            
            with col3:
                st.metric("Processing Time", f"{result.get('processing_time', 0):.1f}s")
            
            st.success("✅ Pocket clustering complete! Analysis finished successfully.")
            
            # Show job ID prominently
            st.markdown(f"""
            <div class="job-id-display">
                🔑 Job ID: {st.session_state.cluster_job_id}
            </div>
            """, unsafe_allow_html=True)
            st.info("💡 Use this Job ID to access your clustering results")
            
            # Show additional result details
            if result.get('stderr'):
                with st.expander("📋 Processing Log"):
                    st.text(result['stderr'])
            
            if result.get('stdout'):
                with st.expander("📋 Output Log"):
                    st.text(result['stdout'])
        
        # Add action buttons - always visible when there's a task
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("❌ Cancel Task", key="cancel_cluster_task"):
                try:
                    if st.session_state.cluster_task_id:
                        task = celery_app.AsyncResult(st.session_state.cluster_task_id)
                        task.revoke(terminate=True)
                    st.session_state.cluster_task_id = None
                    st.session_state.cluster_status = 'cancelled'
                    st.success("Task cancelled successfully!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error cancelling task: {str(e)}")
        
        with col2:
            if st.button("🔍 Check Task Status", key="check_task_status"):
                if st.session_state.cluster_task_id:
                    task = celery_app.AsyncResult(st.session_state.cluster_task_id)
                    st.write(f"**Current Task State:** {task.state}")
                    st.write(f"**Task Ready:** {task.ready()}")
                    if task.ready():
                        st.write(f"**Task Result:** {task.result}")
                        if task.successful():
                            st.session_state.cluster_status = 'completed'
                            st.rerun()
                else:
                    st.write("**No active task ID**")
                st.rerun()
        
        # Show debug info in an expander - always visible
        with st.expander("🔍 Debug Information"):
            st.json(progress_info)
            if st.session_state.cluster_task_id:
                task = celery_app.AsyncResult(st.session_state.cluster_task_id)
                st.write(f"**Task State:** {task.state}")
                st.write(f"**Task ID:** {st.session_state.cluster_task_id}")
                st.write(f"**Task Ready:** {task.ready()}")
                if task.ready():
                    st.write(f"**Task Result:** {task.result}")
            else:
                st.write("**No active task ID**")
                st.write(f"**Session Status:** {st.session_state.cluster_status}")
                st.write(f"**Job ID:** {st.session_state.cluster_job_id}")
        
        # Auto-refresh logic for running tasks
        if st.session_state.cluster_task_id and task_state == 'PROGRESS':
            import time
            time.sleep(0.1)  # Very short sleep for quick detection
            st.rerun()

# Handle completed tasks that don't have task_id anymore
elif st.session_state.cluster_status == 'completed' and st.session_state.cluster_job_id:
    # Show progress bar for completed tasks too - NEVER let it disappear!
    st.markdown('<div class="status-success">✅ Pocket clustering completed successfully!</div>', unsafe_allow_html=True)
    
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
        st.metric("Current Step", "Pocket clustering completed successfully!")
    
    # Display results from output files
    output_dir = os.path.join(RESULTS_DIR, st.session_state.cluster_job_id, "clusters")
    if os.path.exists(output_dir):
        st.markdown("### 📈 Results")
        
        # Display summary metrics
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.metric("Output Directory", os.path.basename(output_dir))
        
        with col2:
            st.metric("Status", "Completed")
        
        with col3:
            st.metric("Analysis", "Finished")
        
        st.success("✅ Pocket clustering complete! Analysis finished successfully.")
        
        # Show job ID prominently
        st.markdown(f"""
        <div class="job-id-display">
            🔑 Job ID: {st.session_state.cluster_job_id}
        </div>
        """, unsafe_allow_html=True)
        st.info("💡 Use this Job ID to access your clustering results")

# Debug section to understand what's happening
with st.expander("🐛 Debug Session State"):
    st.write("**Session State Debug Info:**")
    st.write(f"cluster_task_id: {st.session_state.get('cluster_task_id', 'None')}")
    st.write(f"cluster_status: {st.session_state.get('cluster_status', 'None')}")
    st.write(f"cluster_job_id: {st.session_state.get('cluster_job_id', 'None')}")
    st.write(f"cached_job_ids: {st.session_state.get('cached_job_ids', {})}")
    
    # Check if we have any task activity
    has_task_id = bool(st.session_state.get('cluster_task_id'))
    has_running_status = st.session_state.get('cluster_status') == 'running'
    has_completed_status = st.session_state.get('cluster_status') == 'completed'
    has_job_id = bool(st.session_state.get('cluster_job_id'))
    
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