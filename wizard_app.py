"""
wizard_app.py — PocketHunter Suite linear wizard.

Stages:
  setup            → user fills form, clicks "Run Pipeline"
  pipeline_running → pipeline Celery task in progress
  pipeline_done    → pipeline complete, waiting for actives/decoys upload
  disc_ready       → (alias: pipeline_done, discrimination inputs provided)
  disc_running     → discrimination Celery task in progress
  complete         → all done, results shown
  error            → task failure
"""

import os
import uuid
import json
import zipfile
import time
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import py3Dmol
import streamlit.components.v1 as components
from datetime import datetime
from pathlib import Path

from tasks import run_pockethunter_pipeline, run_discrimination_task
from celery_app import celery_app
from config import Config
from session_state import initialize_session_state
from security import handle_file_upload_secure, SecurityError
from rate_limiter import RateLimitExceeded, check_task_rate_limit
from logging_config import setup_logging

RESULTS_DIR = str(Config.RESULTS_DIR)
UPLOAD_DIR  = str(Config.UPLOAD_DIR)
logger = setup_logging(__name__)

initialize_session_state()

# ── Helpers ──────────────────────────────────────────────────────────────────

def _step_circle(number: str, state: str) -> str:
    """Return HTML for a step circle. state: 'done' | 'active' | 'locked'."""
    cls = f"ph-step-circle ph-step-{state}"
    inner = "&#10003;" if state == "done" else number
    return f'<div class="{cls}">{inner}</div>'


def _step_label(text: str, state: str) -> str:
    cls = "ph-step-label"
    if state == "done":
        cls += " ph-label-done"
    elif state == "active":
        cls += " ph-label-active"
    return f'<div class="{cls}">{text}</div>'


def _line(done: bool) -> str:
    cls = "ph-step-line ph-line-done" if done else "ph-step-line"
    return f'<div class="{cls}"></div>'


def _render_step_strip(active_step: int) -> None:
    """Render the 4-node step progress strip.
    active_step: 1 = setup, 2 = pipeline, 3 = discrimination, 4 = results.
    """
    def _state(step_n: int) -> str:
        if active_step > step_n:
            return "done"
        if active_step == step_n:
            return "active"
        return "locked"

    steps = [
        (1, "Setup"),
        (2, "Extract · Detect · Cluster"),
        (3, "Discrimination"),
        (4, "Results"),
    ]

    nodes_html = ""
    for i, (n, label) in enumerate(steps):
        s = _state(n)
        nodes_html += f'<div class="ph-step-node">{_step_circle(str(n), s)}{_step_label(label, s)}</div>'
        if i < len(steps) - 1:
            nodes_html += _line(active_step > n)

    st.markdown(f'<div class="ph-step-strip">{nodes_html}</div>', unsafe_allow_html=True)


def _badge(text: str, kind: str) -> str:
    """kind: 'active' | 'done' | 'locked' | 'ready'"""
    return f'<span class="ph-badge ph-badge-{kind}">{text}</span>'


def _panel_open(extra_class: str = "") -> None:
    cls = f"ph-panel {extra_class}".strip()
    st.markdown(f'<div class="{cls}">', unsafe_allow_html=True)


def _panel_close() -> None:
    st.markdown('</div>', unsafe_allow_html=True)


def _panel_body_open() -> None:
    st.markdown('<div class="ph-panel-body">', unsafe_allow_html=True)


def _panel_body_close() -> None:
    st.markdown('</div>', unsafe_allow_html=True)


def _panel_header(title: str, badge_text: str, badge_kind: str) -> None:
    st.markdown(
        f'<div class="ph-panel-header">'
        f'<p class="ph-panel-title">{title}</p>'
        f'{_badge(badge_text, badge_kind)}'
        f'</div>',
        unsafe_allow_html=True,
    )


# ── Step 1: Setup form ───────────────────────────────────────────────────────

def _render_step1_form() -> None:
    """Render Step 1 — input files + parameters form."""
    _panel_open("ph-panel-active")
    _panel_header("Step 1 — Input Files &amp; Parameters", "Active", "active")
    _panel_body_open()

    col_traj, col_topo = st.columns(2)
    with col_traj:
        st.markdown("**Trajectory file (.xtc)**")
        traj_file = st.file_uploader(
            "Trajectory", type=["xtc"], label_visibility="collapsed", key="wiz_traj"
        )
    with col_topo:
        st.markdown("**Topology file (.pdb / .gro)**")
        topo_file = st.file_uploader(
            "Topology", type=["pdb", "gro"], label_visibility="collapsed", key="wiz_topo"
        )

    col_stride, col_threads = st.columns(2)
    with col_stride:
        stride = st.slider("Frame stride", 1, 100, 10,
                           help="Extract every Nth frame from the trajectory", key="wiz_stride")
    with col_threads:
        threads = st.slider("CPU threads", 1, 16, 4,
                            help="Parallel threads for pocket detection", key="wiz_threads")

    col_prob, col_method = st.columns(2)
    with col_prob:
        min_prob = st.slider("Min pocket probability", 0.0, 1.0, 0.5, 0.05,
                             help="p2rank probability threshold", key="wiz_min_prob")
    with col_method:
        clustering_method = st.selectbox(
            "Clustering method", ["dbscan", "kmeans", "hierarchical"], key="wiz_cluster_method"
        )

    _panel_body_close()
    _panel_close()

    # Launch button (outside panel so it spans full width cleanly)
    if st.button("Run Pipeline", type="primary", use_container_width=True, key="wiz_launch"):
        _launch_pipeline(traj_file, topo_file, stride, threads, min_prob, clustering_method)


def _launch_pipeline(traj_file, topo_file, stride, threads, min_prob, clustering_method) -> None:
    if not traj_file:
        st.error("Please upload a trajectory (.xtc) file.")
        return
    if not topo_file:
        st.error("Please upload a topology (.pdb or .gro) file.")
        return

    try:
        check_task_rate_limit()
    except RateLimitExceeded as e:
        st.error(f"Rate limit: {e}")
        return

    job_id = f"pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    try:
        traj_path = str(handle_file_upload_secure(traj_file, job_id, "trajectory_"))
        topo_path = str(handle_file_upload_secure(topo_file, job_id, "topology_"))
    except RateLimitExceeded as e:
        st.error(f"Rate limit exceeded: {e}")
        return
    except SecurityError as e:
        st.error(f"File upload failed: {e}")
        return
    except Exception as e:
        st.error(f"Unexpected error during file upload: {e}")
        logger.error(f"Upload error for job {job_id}: {e}", exc_info=True)
        return

    task = run_pockethunter_pipeline.delay(
        xtc_file_path=traj_path,
        topology_file_path=topo_path,
        job_id=job_id,
        stride=stride,
        num_threads=threads,
        min_prob=min_prob,
        clustering_method=clustering_method,
    )

    st.session_state.wiz_job_id   = job_id
    st.session_state.wiz_task_id  = task.id
    st.session_state.wiz_stage    = 'pipeline_running'
    st.session_state.cached_job_ids['pipeline'] = job_id
    st.rerun()


# ── Locked panel stub ────────────────────────────────────────────────────────

def _render_locked_panel(title: str) -> None:
    _panel_open("ph-panel-locked")
    _panel_header(title, "Locked", "locked")
    _panel_close()


# ── Resume bar ───────────────────────────────────────────────────────────────

def _render_resume_bar() -> None:
    """Resume bar shown when no active session."""
    st.markdown('<div class="ph-resume">', unsafe_allow_html=True)
    st.markdown(
        '<div class="ph-resume-label">Resume a previous analysis</div>'
        '<div class="ph-resume-desc">Enter a Job ID to continue where you left off</div>',
        unsafe_allow_html=True,
    )
    col_input, col_btn = st.columns([4, 1])
    with col_input:
        resume_id = st.text_input(
            "Job ID",
            placeholder="pipeline_20240408_ab3f9c12",
            label_visibility="collapsed",
            key="wiz_resume_input",
        )
    with col_btn:
        if st.button("Resume", key="wiz_resume_btn", use_container_width=True):
            _resume_from_job_id(resume_id.strip())
    st.markdown('</div>', unsafe_allow_html=True)


def _resume_from_job_id(job_id: str) -> None:
    """Detect wizard stage from disk and restore session state."""
    if not job_id:
        st.error("Please enter a Job ID.")
        return
    state = _detect_stage_from_disk(job_id, RESULTS_DIR)
    if not state['valid']:
        st.error(f"No results found for Job ID: {job_id}")
        return

    st.session_state.wiz_job_id  = job_id
    st.session_state.wiz_stage   = state['stage']
    st.session_state.wiz_task_id = state.get('pipeline_task_id')
    if state.get('pipeline_result'):
        st.session_state.wiz_pipeline_result = state['pipeline_result']
    if state.get('disc_job_id'):
        st.session_state.wiz_disc_job_id  = state['disc_job_id']
        st.session_state.wiz_disc_task_id = state.get('disc_task_id')
    st.rerun()


# Stage detection placeholder — implemented in Task 7
def _detect_stage_from_disk(job_id: str, results_dir: str) -> dict:
    """Implemented in Task 7."""
    return {'valid': False, 'stage': 'unknown'}


def _render_job_banner(job_id: str, show_warn: bool = True) -> None:
    warn_html = (
        '<div class="ph-job-warn">Your job continues running if you close this tab. '
        'Save this ID to resume later.</div>'
    ) if show_warn else ""
    st.markdown(
        f'<div class="ph-job-banner">'
        f'<div class="ph-job-label">Job ID — save this</div>'
        f'<div class="ph-job-value">{job_id}</div>'
        f'{warn_html}'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.code(job_id, language=None)  # one-click copy via Streamlit code block


def _render_done_panel_step1() -> None:
    _panel_open("ph-panel-done")
    _panel_header("Step 1 — Input Files &amp; Parameters", "Done", "done")
    _panel_close()


def _pipeline_stage_chips(stage_name: str) -> str:
    """Return HTML chips for Extract / Detect / Cluster based on current stage."""
    stages = [
        ('extract', 'Frame extraction'),
        ('detect',  'Pocket detection'),
        ('cluster', 'Clustering'),
    ]
    html = ""
    for key, label in stages:
        if stage_name in ('cluster', 'cluster_done') and key in ('extract', 'detect'):
            cls = "ph-chip-done"
        elif stage_name == 'detect' and key == 'extract':
            cls = "ph-chip-done"
        elif stage_name == key or (stage_name == 'cluster_done' and key == 'cluster'):
            cls = "ph-chip-active"
        else:
            cls = "ph-chip-wait"
        html += f'<span class="ph-chip {cls}">{label}</span>'
    return html


def _render_pipeline_progress(task_id: str, job_id: str) -> None:
    """Poll the pipeline Celery task and render progress. Reruns every 3 s while running."""
    task = celery_app.AsyncResult(task_id)
    state = task.state
    meta  = task.info or {}

    _render_job_banner(job_id)
    _render_done_panel_step1()

    _panel_open("ph-panel-active")
    _panel_header("Step 2 — Extract · Detect · Cluster", "Running", "active")
    _panel_body_open()

    if state == 'PENDING':
        st.markdown(
            '<div class="ph-prog-meta">'
            '<span class="ph-prog-text">Waiting in queue…</span>'
            '<span class="ph-prog-pct">0%</span></div>'
            '<div class="ph-prog-outer"><div class="ph-prog-inner" style="width:0%"></div></div>',
            unsafe_allow_html=True,
        )
        _panel_body_close()
        _panel_close()
        _render_locked_panel("Step 3 — Discrimination Analysis")
        time.sleep(4)
        st.rerun()

    elif state == 'PROGRESS':
        pct       = meta.get('progress', 0)
        step_text = meta.get('current_step', 'Processing…')
        stage_key = meta.get('stage', '')
        frames    = meta.get('frames_extracted')
        pockets   = meta.get('pockets_detected')

        st.markdown(
            f'<div class="ph-prog-meta">'
            f'<span class="ph-prog-text">{step_text}</span>'
            f'<span class="ph-prog-pct">{pct}%</span>'
            f'</div>'
            f'<div class="ph-prog-outer"><div class="ph-prog-inner" style="width:{pct}%"></div></div>',
            unsafe_allow_html=True,
        )
        st.markdown(_pipeline_stage_chips(stage_key), unsafe_allow_html=True)

        log_lines = []
        if frames is not None:
            log_lines.append(f"Extracted {frames} frames → pdbs/")
        if pockets is not None:
            log_lines.append(f"{pockets} pockets detected so far")
        if log_lines:
            lines_html = "<br>".join(log_lines)
            st.markdown(
                f'<div class="ph-log"><div class="ph-log-label">Log</div>{lines_html}</div>',
                unsafe_allow_html=True,
            )

        _panel_body_close()
        _panel_close()
        _render_locked_panel("Step 3 — Discrimination Analysis")
        time.sleep(3)
        st.rerun()

    elif state == 'SUCCESS':
        result = task.result or {}
        _panel_body_close()
        _panel_close()
        st.session_state.wiz_pipeline_result = result
        st.session_state.wiz_stage = 'pipeline_done'
        st.session_state.cached_job_ids['pipeline'] = job_id
        st.session_state.cached_job_ids['cluster']  = result.get('cluster_job_id', job_id)
        st.rerun()

    elif state in ('FAILURE', 'REVOKED'):
        err = meta.get('exc_message', str(meta)) if isinstance(meta, dict) else str(meta)
        st.error(f"Pipeline failed: {err}")
        _panel_body_close()
        _panel_close()
        st.session_state.wiz_stage = 'error'
        if st.button("Start over", key="wiz_restart_from_error"):
            for k in ('wiz_stage','wiz_job_id','wiz_task_id','wiz_pipeline_result',
                      'wiz_disc_job_id','wiz_disc_task_id'):
                st.session_state[k] = None if k != 'wiz_stage' else 'setup'
            st.rerun()

    else:
        _panel_body_close()
        _panel_close()
        time.sleep(3)
        st.rerun()


def _render_pipeline_done_panel(result: dict) -> None:
    """Collapsed Step 2 panel showing pipeline metrics."""
    frames = result.get('frames_extracted', '—')
    pockets = result.get('pockets_detected', '—')
    reps = result.get('representatives', '—')

    _panel_open("ph-panel-done")
    _panel_header("Step 2 — Extract · Detect · Cluster", "Done", "done")
    st.markdown('<div class="ph-panel-body">', unsafe_allow_html=True)
    m1, m2, m3 = st.columns(3)
    with m1:
        st.markdown(
            f'<div class="ph-metric"><div class="ph-metric-label">Frames</div>'
            f'<div class="ph-metric-value">{frames}</div></div>',
            unsafe_allow_html=True,
        )
    with m2:
        st.markdown(
            f'<div class="ph-metric"><div class="ph-metric-label">Pockets detected</div>'
            f'<div class="ph-metric-value">{pockets}</div></div>',
            unsafe_allow_html=True,
        )
    with m3:
        st.markdown(
            f'<div class="ph-metric"><div class="ph-metric-label">Cluster representatives</div>'
            f'<div class="ph-metric-value">{reps}</div></div>',
            unsafe_allow_html=True,
        )
    st.markdown('</div></div>', unsafe_allow_html=True)


def _render_disc_form(pipeline_job_id: str) -> None:
    """Step 3 form: upload actives + decoys, launch discrimination."""
    result = st.session_state.wiz_pipeline_result or {}
    cluster_job_id = result.get('cluster_job_id', pipeline_job_id)
    reps_csv = os.path.join(RESULTS_DIR, cluster_job_id, 'pocket_clusters', 'cluster_representatives.csv')
    n_reps = 0
    if os.path.exists(reps_csv):
        try:
            n_reps = len(pd.read_csv(reps_csv))
        except Exception:
            pass

    _panel_open("ph-panel-active")
    _panel_header("Step 3 — Discrimination Analysis", "Ready", "ready")
    st.markdown('<div class="ph-panel-body">', unsafe_allow_html=True)

    if n_reps:
        st.markdown(
            f'<div style="font-size:12px;color:#888;margin-bottom:12px;">'
            f'Upload active and decoy ligand sets to rank {n_reps} cluster representatives.'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div style="font-size:12px;color:#888;margin-bottom:12px;">'
            'Upload active and decoy ligand sets to rank cluster representatives.'
            '</div>',
            unsafe_allow_html=True,
        )

    col_act, col_dec = st.columns(2)
    with col_act:
        st.markdown("**Active ligands (.sdf) — max 200**")
        actives_file = st.file_uploader(
            "Actives", type=["sdf"], label_visibility="collapsed", key="wiz_actives"
        )
    with col_dec:
        st.markdown("**Decoy ligands (.sdf) — max 2000**")
        decoys_file = st.file_uploader(
            "Decoys", type=["sdf"], label_visibility="collapsed", key="wiz_decoys"
        )

    with st.expander("Decoy quality guidance"):
        st.markdown("""
**Pharmacophore complementarity is a broad-class discriminator.**

| Decoy type | Expected ROC-AUC |
|---|---|
| Drug-like (other targets) | ~0.5–0.6 |
| Property-matched (DUD-E) | ~0.55–0.70 |
| Diverse / non-drug-like | ~0.7–0.9 |

Focus on the **relative ranking** of conformations rather than absolute ROC-AUC value.
        """)

    st.markdown('</div></div>', unsafe_allow_html=True)

    can_launch = actives_file is not None and decoys_file is not None
    if st.button("Run Discrimination Analysis", type="primary",
                 disabled=not can_launch, use_container_width=True, key="wiz_disc_launch"):
        _launch_discrimination(cluster_job_id, actives_file, decoys_file, pipeline_job_id)


def _launch_discrimination(cluster_job_id, actives_file, decoys_file, pipeline_job_id) -> None:
    disc_job_id = f"disc_{uuid.uuid4().hex[:8]}"
    upload_dir  = os.path.join(UPLOAD_DIR, disc_job_id)
    os.makedirs(upload_dir, exist_ok=True)

    actives_path = os.path.join(upload_dir, 'actives.sdf')
    decoys_path  = os.path.join(upload_dir, 'decoys.sdf')
    with open(actives_path, 'wb') as f:
        f.write(actives_file.getbuffer())
    with open(decoys_path, 'wb') as f:
        f.write(decoys_file.getbuffer())

    extract_job_id = st.session_state.cached_job_ids.get('extract') or None
    task = run_discrimination_task.delay(
        cluster_job_id=cluster_job_id,
        actives_path=actives_path,
        decoys_path=decoys_path,
        job_id=disc_job_id,
        extract_job_id=extract_job_id,
    )

    st.session_state.wiz_disc_job_id  = disc_job_id
    st.session_state.wiz_disc_task_id = task.id
    st.session_state.wiz_stage        = 'disc_running'
    st.session_state.cached_job_ids['discrimination'] = disc_job_id
    st.rerun()


def _render_disc_progress(disc_task_id: str, disc_job_id: str, pipeline_job_id: str) -> None:
    """Poll discrimination Celery task and render progress."""
    task  = celery_app.AsyncResult(disc_task_id)
    state = task.state
    meta  = task.info or {}

    result = st.session_state.wiz_pipeline_result or {}
    _render_job_banner(pipeline_job_id, show_warn=False)
    _render_done_panel_step1()
    _render_pipeline_done_panel(result)

    _panel_open("ph-panel-active")
    _panel_header("Step 3 — Discrimination Analysis", "Running", "active")
    st.markdown('<div class="ph-panel-body">', unsafe_allow_html=True)

    if state in ('PENDING', 'PROGRESS'):
        pct  = meta.get('progress', 0) if state == 'PROGRESS' else 0
        text = meta.get('current_step', 'Running…') if state == 'PROGRESS' else 'Queued…'
        st.markdown(
            f'<div class="ph-prog-meta">'
            f'<span class="ph-prog-text">{text}</span>'
            f'<span class="ph-prog-pct">{pct}%</span></div>'
            f'<div class="ph-prog-outer"><div class="ph-prog-inner" style="width:{pct}%"></div></div>',
            unsafe_allow_html=True,
        )
        st.markdown('</div></div>', unsafe_allow_html=True)
        time.sleep(3)
        st.rerun()

    elif state == 'SUCCESS':
        st.session_state.wiz_stage = 'complete'
        st.markdown('</div></div>', unsafe_allow_html=True)
        st.rerun()

    elif state in ('FAILURE', 'REVOKED'):
        err = meta.get('exc_message', str(meta)) if isinstance(meta, dict) else str(meta)
        st.error(f"Discrimination failed: {err}")
        st.markdown('</div></div>', unsafe_allow_html=True)
        st.session_state.wiz_stage = 'error'
        if st.button("Retry discrimination", key="wiz_disc_retry"):
            st.session_state.wiz_stage        = 'pipeline_done'
            st.session_state.wiz_disc_task_id = None
            st.session_state.wiz_disc_job_id  = None
            st.rerun()

    else:
        st.markdown('</div></div>', unsafe_allow_html=True)
        time.sleep(3)
        st.rerun()


# ── Page render ──────────────────────────────────────────────────────────────

stage = st.session_state.wiz_stage

if stage == 'setup':
    _render_resume_bar()
    _render_step_strip(1)
    _render_step1_form()
    _render_locked_panel("Step 2 — Extract · Detect · Cluster")
    _render_locked_panel("Step 3 — Discrimination Analysis")

elif stage == 'pipeline_running':
    _render_step_strip(2)
    _render_pipeline_progress(
        st.session_state.wiz_task_id,
        st.session_state.wiz_job_id,
    )

elif stage == 'pipeline_done':
    _render_step_strip(3)
    _render_job_banner(st.session_state.wiz_job_id, show_warn=False)
    _render_done_panel_step1()
    _render_pipeline_done_panel(st.session_state.wiz_pipeline_result or {})
    _render_disc_form(st.session_state.wiz_job_id)

elif stage == 'disc_running':
    _render_step_strip(3)
    _render_disc_progress(
        st.session_state.wiz_disc_task_id,
        st.session_state.wiz_disc_job_id,
        st.session_state.wiz_job_id,
    )

else:
    st.info("Step 4 results (Task 6) — coming soon.")
