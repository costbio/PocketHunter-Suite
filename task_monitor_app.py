import streamlit as st
import os
import pandas as pd
import plotly.express as px
from datetime import datetime, timedelta
import time
import json
import glob
from celery_app import celery_app
from config import Config

# Use Config for directories
RESULTS_DIR = str(Config.RESULTS_DIR)

# Helper functions
def get_all_job_statuses():
    """Get all job status files and their information"""
    status_files = glob.glob(os.path.join(RESULTS_DIR, "*_status.json"))
    jobs = []
    
    for status_file in status_files:
        try:
            with open(status_file, 'r') as f:
                status_data = json.load(f)
            
            job_id = os.path.basename(status_file).replace('_status.json', '')
            status_data['job_id'] = job_id
            status_data['status_file'] = status_file
            
            # Get task info if available
            if 'task_id' in status_data:
                try:
                    task = celery_app.AsyncResult(status_data['task_id'])
                    status_data['task_state'] = task.state
                    status_data['task_info'] = task.info
                except Exception:
                    status_data['task_state'] = 'UNKNOWN'
                    status_data['task_info'] = None
            
            jobs.append(status_data)
        except Exception as e:
            st.error(f"Error reading status file {status_file}: {str(e)}")
    
    return jobs

def format_duration(seconds):
    """Format duration in seconds to human readable string"""
    if seconds is None:
        return "N/A"
    
    duration = timedelta(seconds=seconds)
    if duration.days > 0:
        return f"{duration.days}d {duration.seconds//3600}h {(duration.seconds%3600)//60}m"
    elif duration.seconds > 3600:
        return f"{duration.seconds//3600}h {(duration.seconds%3600)//60}m"
    elif duration.seconds > 60:
        return f"{duration.seconds//60}m {duration.seconds%60}s"
    else:
        return f"{duration.seconds}s"

def get_job_type(job_id):
    """Determine job type from job ID"""
    if job_id.startswith('full_pipeline'):
        return 'Full Pipeline'
    elif job_id.startswith('extract'):
        return 'Extract Frames'
    elif job_id.startswith('detect'):
        return 'Detect Pockets'
    elif job_id.startswith('cluster'):
        return 'Cluster Pockets'
    elif job_id.startswith('dock'):
        return 'Molecular Docking'
    else:
        return 'Unknown'

def get_related_jobs(job_id, all_jobs):
    """Get jobs related to the given job ID (upstream and downstream)"""
    related = set([job_id])

    # Find the job data
    job_data = next((j for j in all_jobs if j.get('job_id') == job_id), None)
    if not job_data:
        return related

    # Extract base timestamp from job_id (format: type_YYYYMMDD_HHMMSS_hash)
    parts = job_id.split('_')
    if len(parts) >= 3:
        timestamp = f"{parts[1]}_{parts[2]}"  # YYYYMMDD_HHMMSS

        # Find jobs with same timestamp (they're from the same pipeline run)
        for job in all_jobs:
            other_id = job.get('job_id', '')
            if timestamp in other_id:
                related.add(other_id)

    return related

# Main UI
st.markdown("""
<div class="metric-card">
    <h2>📊 Task Monitor</h2>
    <p>Monitor all running and completed PocketHunter tasks</p>
</div>
""", unsafe_allow_html=True)

# Controls row
col1, col2 = st.columns([3, 1])
with col1:
    auto_refresh = st.checkbox("🔄 Auto-refresh (every 5 seconds)", value=True)
with col2:
    show_all = st.checkbox("Show all jobs", value=False, help="Show all jobs instead of only cached ones")

# Search section
st.markdown("### 🔍 Search Jobs")
search_job_id = st.text_input(
    "Search by Job ID (shows related jobs too):",
    placeholder="e.g., cluster_20251213_011228_4c51c6a5",
    help="Enter a job ID to find it and all related pipeline jobs"
)

# Get all jobs
all_jobs = get_all_job_statuses()

# Filter jobs based on search or cached status
if search_job_id:
    # Smart search: show the searched job and all related jobs
    related_job_ids = get_related_jobs(search_job_id, all_jobs)
    jobs = [job for job in all_jobs if job.get('job_id', '') in related_job_ids]

    if not jobs:
        st.warning(f"❌ Job ID '{search_job_id}' not found.")
    else:
        st.success(f"✅ Found {len(jobs)} related job(s)")
elif show_all:
    # Show all jobs
    jobs = all_jobs
else:
    # Show only cached jobs (jobs from current session)
    cached_job_ids = st.session_state.get('cached_job_ids', {})
    cached_ids_list = list(cached_job_ids.values())
    jobs = [job for job in all_jobs if job.get('job_id', '') in cached_ids_list]

    if not jobs and cached_ids_list:
        st.info("💡 No cached jobs found in results directory. They may have been deleted.")
    elif not cached_ids_list:
        st.info("💡 No cached jobs yet. Run tasks in other steps to see them here, or check 'Show all jobs' to view everything.")

if jobs:
    # Create DataFrame for display
    job_data = []
    for job in jobs:
        job_data.append({
            'Job ID': job.get('job_id', 'N/A'),
            'Type': get_job_type(job.get('job_id', '')),
            'Status': job.get('status', 'Unknown'),
            'Step': job.get('step', 'N/A'),
            'Task State': job.get('task_state', 'N/A'),
            'Last Updated': job.get('last_updated', 'N/A')
        })
    
    df = pd.DataFrame(job_data)
    
    # Filter options
    st.markdown("### 🔍 Filter Tasks")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        status_filter = st.selectbox(
            "Filter by Status",
            options=['All'] + list(df['Status'].unique()),
            key="status_filter"
        )
    
    with col2:
        type_filter = st.selectbox(
            "Filter by Type",
            options=['All'] + list(df['Type'].unique()),
            key="type_filter"
        )
    
    with col3:
        state_filter = st.selectbox(
            "Filter by Task State",
            options=['All'] + list(df['Task State'].unique()),
            key="state_filter"
        )
    
    # Apply filters
    filtered_df = df.copy()
    if status_filter != 'All':
        filtered_df = filtered_df[filtered_df['Status'] == status_filter]
    if type_filter != 'All':
        filtered_df = filtered_df[filtered_df['Type'] == type_filter]
    if state_filter != 'All':
        filtered_df = filtered_df[filtered_df['Task State'] == state_filter]
    
    # Display summary metrics
    st.markdown("### 📈 Summary")

    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        # Show current view mode
        if search_job_id:
            st.metric("View Mode", "Search")
        elif show_all:
            st.metric("View Mode", "All Jobs")
        else:
            cached_count = len(st.session_state.get('cached_job_ids', {}).values())
            st.metric("Cached Jobs", cached_count)

    with col2:
        total_jobs = len(df)
        st.metric("Showing", total_jobs)

    with col3:
        running_jobs = len(df[df['Status'].isin(['running', 'submitted'])])
        st.metric("Running", running_jobs)

    with col4:
        completed_jobs = len(df[df['Status'] == 'completed'])
        st.metric("Completed", completed_jobs)

    with col5:
        failed_jobs = len(df[df['Status'] == 'failed'])
        st.metric("Failed", failed_jobs)
    
    # Display tasks table
    st.markdown("### 📋 Task Details")
    
    if not filtered_df.empty:
        # Style the dataframe
        def color_status(val):
            if val == 'completed':
                return 'background-color: #d4edda; color: #155724'
            elif val == 'running':
                return 'background-color: #d1ecf1; color: #0c5460'
            elif val == 'failed':
                return 'background-color: #f8d7da; color: #721c24'
            elif val == 'submitted':
                return 'background-color: #fff3cd; color: #856404'
            else:
                return ''
        
        styled_df = filtered_df.style.applymap(color_status, subset=['Status'])
        st.dataframe(styled_df, use_container_width=True)
        
        # Detailed view for selected job
        st.markdown("### 🔍 Detailed View")
        selected_job_id = st.selectbox(
            "Select a job for detailed information:",
            options=filtered_df['Job ID'].tolist(),
            key="selected_job"
        )
        
        if selected_job_id:
            selected_job = next((job for job in jobs if job.get('job_id') == selected_job_id), None)
            
            if selected_job:
                col1, col2 = st.columns(2)
                
                with col1:
                    st.markdown("#### Job Information")
                    st.write(f"**Job ID:** {selected_job.get('job_id', 'N/A')}")
                    st.write(f"**Type:** {get_job_type(selected_job.get('job_id', ''))}")
                    st.write(f"**Status:** {selected_job.get('status', 'N/A')}")
                    st.write(f"**Step:** {selected_job.get('step', 'N/A')}")
                    st.write(f"**Task State:** {selected_job.get('task_state', 'N/A')}")
                    
                    if 'last_updated' in selected_job:
                        try:
                            last_updated = datetime.fromisoformat(selected_job['last_updated'])
                            st.write(f"**Last Updated:** {last_updated.strftime('%Y-%m-%d %H:%M:%S')}")
                        except (ValueError, TypeError):
                            st.write(f"**Last Updated:** {selected_job['last_updated']}")
                
                with col2:
                    st.markdown("#### Task Information")
                    if 'task_id' in selected_job:
                        st.write(f"**Task ID:** {selected_job['task_id']}")
                        
                        try:
                            task = celery_app.AsyncResult(selected_job['task_id'])
                            st.write(f"**Task State:** {task.state}")
                            
                            if task.info:
                                if isinstance(task.info, dict):
                                    for key, value in task.info.items():
                                        if key == 'progress':
                                            # Type check to prevent crash on non-numeric values
                                            if isinstance(value, (int, float)):
                                                st.write(f"**Progress:** {value:.1f}%")
                                            else:
                                                st.write(f"**Progress:** {value}")
                                        elif key == 'current_step':
                                            st.write(f"**Current Step:** {value}")
                                        else:
                                            st.write(f"**{key.title()}:** {value}")
                                else:
                                    st.write(f"**Task Info:** {task.info}")
                        except Exception as e:
                            st.error(f"Error getting task info: {str(e)}")
                
                # Show result information if available
                if 'result_info' in selected_job:
                    st.markdown("#### 📊 Results")
                    result_info = selected_job['result_info']
                    
                    if isinstance(result_info, dict):
                        # Display metrics
                        metrics_cols = st.columns(min(4, len(result_info)))
                        for i, (key, value) in enumerate(result_info.items()):
                            with metrics_cols[i % len(metrics_cols)]:
                                st.metric(key.replace('_', ' ').title(), value)
                    
                    # Show output files if available
                    if 'output_files' in result_info:
                        st.markdown("**Generated Files:**")
                        for file_path in result_info['output_files']:
                            if os.path.exists(file_path):
                                file_name = os.path.basename(file_path)
                                st.write(f"• {file_name}")
                
                # Action buttons
                st.markdown("#### ⚡ Actions")
                
                col1, col2, col3 = st.columns(3)
                
                with col1:
                    if st.button("🔄 Refresh Job Status", key=f"refresh_{selected_job_id}"):
                        st.rerun()
                
                with col2:
                    if st.button("🗑️ Clear Job Data", key=f"clear_{selected_job_id}"):
                        # Remove status file
                        status_file = selected_job.get('status_file')
                        if status_file and os.path.exists(status_file):
                            os.remove(status_file)
                        st.success("Job data cleared!")
                        st.rerun()
                
                with col3:
                    if st.button("📥 Download Results", key=f"download_{selected_job_id}"):
                        # Create ZIP of results
                        job_results_dir = os.path.join(RESULTS_DIR, selected_job_id)
                        if os.path.exists(job_results_dir):
                            import zipfile
                            zip_path = os.path.join(RESULTS_DIR, f"{selected_job_id}_results.zip")
                            
                            with zipfile.ZipFile(zip_path, 'w') as zipf:
                                for root, dirs, files in os.walk(job_results_dir):
                                    for file in files:
                                        file_path = os.path.join(root, file)
                                        arcname = os.path.relpath(file_path, job_results_dir)
                                        zipf.write(file_path, arcname)
                            
                            with open(zip_path, 'rb') as f:
                                st.download_button(
                                    label="Download Results (ZIP)",
                                    data=f.read(),
                                    file_name=f"{selected_job_id}_results.zip",
                                    mime="application/zip",
                                    key=f"download_results_{selected_job_id}"
                                )
    else:
        st.info("No tasks match the selected filters.")

# Auto-refresh
if auto_refresh:
    time.sleep(5)
    st.rerun() 