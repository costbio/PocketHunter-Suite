import streamlit as st
import os
import pandas as pd
from datetime import datetime
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
    elif job_id.startswith('pipeline_'):
        return 'Pipeline'
    elif job_id.startswith('disc_'):
        return 'Discrimination'
    else:
        return 'Unknown'

def get_related_jobs(job_id, all_jobs):
    """Get jobs related to the given job ID (upstream and downstream)"""
    related = set()

    # Find the job data — try exact match first, then substring match
    job_data = next((j for j in all_jobs if j.get('job_id') == job_id), None)
    if not job_data:
        job_data = next((j for j in all_jobs if job_id in j.get('job_id', '')), None)
    if job_data:
        related.add(job_data['job_id'])
    else:
        related.add(job_id)
        return related

    # Extract timestamp from the matched job_id (format: type_YYYYMMDD_HHMMSS_hash)
    matched_id = job_data['job_id']
    parts = matched_id.split('_')
    # Find YYYYMMDD_HHMMSS pattern in parts
    for i in range(len(parts) - 1):
        if len(parts[i]) == 8 and parts[i].isdigit() and len(parts[i+1]) == 6 and parts[i+1].isdigit():
            timestamp = f"{parts[i]}_{parts[i+1]}"
            # Find jobs with same timestamp (from the same pipeline run)
            for job in all_jobs:
                other_id = job.get('job_id', '')
                if timestamp in other_id:
                    related.add(other_id)
            break

    return related

# Main UI
st.markdown(
    '<div class="ph-panel ph-panel-active" style="padding:14px 18px;margin-bottom:18px;">'
    '<span style="font-size:15px;font-weight:600;color:#2a3a4a;">Task Monitor</span>'
    '<span style="font-size:12px;color:#9aa0b8;margin-left:10px;">All running and completed PocketHunter jobs</span>'
    '</div>',
    unsafe_allow_html=True,
)

# Controls
col_ctl1, col_ctl2 = st.columns([3, 1])
with col_ctl1:
    auto_refresh = st.checkbox("Auto-refresh (every 5 seconds)", value=True)
with col_ctl2:
    show_all = st.checkbox("Show all jobs", value=False, help="Show all jobs instead of only session jobs")

# Search
st.markdown('<div style="font-size:13px;font-weight:600;color:#2a3a4a;margin:18px 0 8px;">Search</div>', unsafe_allow_html=True)
search_job_id = st.text_input(
    "Job ID",
    placeholder="e.g., pipeline_20260408_ab3f9c12",
    label_visibility="collapsed",
    help="Enter a job ID to find it and all related jobs",
    key="tm_search",
)

# Load jobs
all_jobs = get_all_job_statuses()

if search_job_id:
    related_job_ids = get_related_jobs(search_job_id.strip(), all_jobs)
    jobs = [j for j in all_jobs if j.get('job_id', '') in related_job_ids]
    if not jobs:
        st.warning(f"No jobs found for: {search_job_id.strip()}")
    else:
        st.success(f"Found {len(jobs)} related job(s)")
elif show_all:
    jobs = all_jobs
else:
    cached_ids = list(st.session_state.get('cached_job_ids', {}).values())
    jobs = [j for j in all_jobs if j.get('job_id', '') in cached_ids]
    if not jobs and cached_ids:
        st.info("Cached jobs not found in results directory — they may have been deleted.")
    elif not cached_ids:
        st.info("No jobs in this session yet. Enable 'Show all jobs' to browse everything.")

if not jobs:
    if auto_refresh:
        time.sleep(5)
        st.rerun()
    st.stop()

# ── Summary metrics ──────────────────────────────────────────────────────────
total_jobs     = len(jobs)
running_jobs   = sum(1 for j in jobs if j.get('status') in ('running', 'submitted'))
completed_jobs = sum(1 for j in jobs if j.get('status') == 'completed')
failed_jobs    = sum(1 for j in jobs if j.get('status') == 'failed')

m1, m2, m3, m4 = st.columns(4)
for col, label, val in zip(
    [m1, m2, m3, m4],
    ["Total", "Running", "Completed", "Failed"],
    [total_jobs, running_jobs, completed_jobs, failed_jobs],
):
    with col:
        st.markdown(
            f'<div class="ph-metric"><div class="ph-metric-label">{label}</div>'
            f'<div class="ph-metric-value">{val}</div></div>',
            unsafe_allow_html=True,
        )

# ── Jobs table ───────────────────────────────────────────────────────────────
st.markdown('<div style="font-size:13px;font-weight:600;color:#2a3a4a;margin:20px 0 8px;">Jobs</div>', unsafe_allow_html=True)

# Filter bar — Status and Type only
col_f1, col_f2 = st.columns(2)
job_rows = [
    {
        'Job ID':       j.get('job_id', 'N/A'),
        'Type':         get_job_type(j.get('job_id', '')),
        'Status':       j.get('status', 'unknown'),
        'Last Updated': j.get('last_updated', 'N/A'),
    }
    for j in jobs
]
df = pd.DataFrame(job_rows)

with col_f1:
    status_filter = st.selectbox("Status", ['All'] + sorted(df['Status'].unique().tolist()), key="tm_sf")
with col_f2:
    type_filter = st.selectbox("Type", ['All'] + sorted(df['Type'].unique().tolist()), key="tm_tf")

filtered_df = df.copy()
if status_filter != 'All':
    filtered_df = filtered_df[filtered_df['Status'] == status_filter]
if type_filter != 'All':
    filtered_df = filtered_df[filtered_df['Type'] == type_filter]

if filtered_df.empty:
    st.info("No jobs match the selected filters.")
else:
    def color_status(val):
        if val == 'completed':
            return 'background-color: rgba(0,168,133,.10); color: #00a085'
        elif val in ('running', 'submitted'):
            return 'background-color: rgba(45,116,218,.10); color: #2d74da'
        elif val == 'failed':
            return 'background-color: rgba(214,48,49,.10); color: #d63031'
        return 'color: #9aa0b8'

    st.dataframe(
        filtered_df.style.map(color_status, subset=['Status']),
        use_container_width=True,
        hide_index=True,
    )

    # ── Detailed panel ────────────────────────────────────────────────────────
    st.markdown('<div style="font-size:13px;font-weight:600;color:#2a3a4a;margin:20px 0 8px;">Detail</div>', unsafe_allow_html=True)
    selected_job_id = st.selectbox(
        "Select job",
        options=filtered_df['Job ID'].tolist(),
        label_visibility="collapsed",
        key="tm_sel",
    )

    selected_job = next((j for j in jobs if j.get('job_id') == selected_job_id), None)

    if selected_job:
        status = selected_job.get('status', 'unknown')

        # Status badge color
        badge_kind = (
            'done'   if status == 'completed' else
            'active' if status in ('running', 'submitted') else
            'locked' if status == 'failed' else
            'ready'
        )

        _panel_class = 'ph-panel-active' if status in ('running', 'submitted') else 'ph-panel-done'
        st.markdown(f'<div class="ph-panel {_panel_class}">', unsafe_allow_html=True)

        # Header row
        st.markdown(
            f'<div class="ph-panel-header">'
            f'<p class="ph-panel-title">{selected_job_id}</p>'
            f'<span class="ph-badge ph-badge-{badge_kind}">{status}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
        st.markdown('<div class="ph-panel-body">', unsafe_allow_html=True)

        # Meta row
        last_updated = selected_job.get('last_updated', '')
        try:
            last_updated = datetime.fromisoformat(last_updated).strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            pass

        st.markdown(
            f'<div style="font-size:12px;color:#888;margin-bottom:12px;">'
            f'Type: <strong>{get_job_type(selected_job_id)}</strong>'
            f'&nbsp;&nbsp;·&nbsp;&nbsp;Last updated: <strong>{last_updated or "N/A"}</strong>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # Live progress if running
        if status in ('running', 'submitted') and 'task_id' in selected_job:
            try:
                task = celery_app.AsyncResult(selected_job['task_id'])
                meta = task.info or {}
                if isinstance(meta, dict):
                    pct  = meta.get('progress', 0)
                    step = meta.get('current_step', 'Processing...')
                    st.markdown(
                        f'<div class="ph-prog-meta">'
                        f'<span class="ph-prog-text">{step}</span>'
                        f'<span class="ph-prog-pct">{pct}%</span></div>'
                        f'<div class="ph-prog-outer"><div class="ph-prog-inner" style="width:{pct}%"></div></div>',
                        unsafe_allow_html=True,
                    )
            except Exception:
                pass

        # Result metrics (completed jobs) — skip paths, job IDs, CSV filenames
        result_info = selected_job.get('result_info', {})
        if isinstance(result_info, dict):
            _SKIP_SUFFIXES = ('_job_id', '_path', '_dir', '_file', '_csv', '_id')
            def _fmt_val(key, val):
                if key == 'processing_time':
                    v = float(val)
                    if v >= 3600:
                        return f"{int(v//3600)}h {int((v%3600)//60)}m"
                    elif v >= 60:
                        return f"{int(v//60)}m {int(v%60)}s"
                    else:
                        return f"{v:.1f} s"
                if isinstance(val, float):
                    return f"{val:.2f}"
                return str(val)

            numeric_items = [
                (k, _fmt_val(k, v)) for k, v in result_info.items()
                if isinstance(v, (int, float))
                and not any(k.endswith(s) for s in _SKIP_SUFFIXES)
            ]
            if numeric_items:
                cards_html = ''.join(
                    f'<div class="ph-metric" style="flex:1;min-width:0;">'
                    f'<div class="ph-metric-label">{k.replace("_", " ").title()}</div>'
                    f'<div class="ph-metric-value">{v}</div></div>'
                    for k, v in numeric_items
                )
                st.markdown(
                    f'<div style="display:flex;gap:8px;margin-bottom:12px;">{cards_html}</div>',
                    unsafe_allow_html=True,
                )

        st.markdown('</div></div>', unsafe_allow_html=True)

        # Actions
        st.markdown('<div style="font-size:12px;color:#9aa0b8;margin:12px 0 6px;">Actions</div>', unsafe_allow_html=True)
        act1, act2 = st.columns(2)
        with act1:
            if st.button("Refresh", key=f"tm_refresh_{selected_job_id}"):
                st.rerun()
        with act2:
            if st.button("Clear job data", key=f"tm_clear_{selected_job_id}"):
                status_file = selected_job.get('status_file')
                if status_file and os.path.exists(status_file):
                    os.remove(status_file)
                st.success("Job data cleared.")
                st.rerun()

# Auto-refresh
if auto_refresh:
    time.sleep(5)
    st.rerun()
