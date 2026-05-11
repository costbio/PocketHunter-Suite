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
    """
    Read disk state for job_id and return the wizard stage to restore.

    Returns:
        {
          'valid': bool,
          'stage': 'pipeline_running'|'pipeline_done'|'disc_running'|'complete'|'unknown',
          'pipeline_task_id': str|None,
          'pipeline_result': dict|None,
          'disc_job_id': str|None,
          'disc_task_id': str|None,
        }
    """
    status_file = os.path.join(results_dir, f"{job_id}_status.json")
    if not os.path.exists(status_file):
        return {'valid': False, 'stage': 'unknown'}

    try:
        with open(status_file) as f:
            status = json.load(f)
    except Exception:
        return {'valid': False, 'stage': 'unknown'}

    pipeline_task_id = status.get('task_id')
    result_info      = status.get('result_info', {})
    pipeline_status  = status.get('status', '')
    disc_job_id      = status.get('disc_job_id')
    disc_task_id     = status.get('disc_task_id')

    base = {
        'valid': True,
        'pipeline_task_id': pipeline_task_id,
        'pipeline_result': result_info or None,
        'disc_job_id': disc_job_id,
        'disc_task_id': disc_task_id,
    }

    # Pipeline still running
    if pipeline_status == 'running':
        return {**base, 'stage': 'pipeline_running'}

    # Pipeline done — check for cluster_representatives.csv
    reps_csv = os.path.join(results_dir, job_id, 'pocket_clusters', 'cluster_representatives.csv')
    if not os.path.exists(reps_csv):
        # Pipeline completed but no cluster results yet — treat as still running
        return {**base, 'stage': 'pipeline_running'}

    # No discrimination started
    if not disc_job_id:
        return {**base, 'stage': 'pipeline_done'}

    # Discrimination started — check its status file
    disc_status_file = os.path.join(results_dir, f"{disc_job_id}_status.json")
    if not os.path.exists(disc_status_file):
        return {**base, 'stage': 'disc_running'}

    try:
        with open(disc_status_file) as f:
            disc_status = json.load(f)
    except Exception:
        return {**base, 'stage': 'disc_running'}

    if disc_status.get('status') == 'completed':
        disc_csv = disc_status.get('result_info', {}).get('discrimination_results_csv')
        if disc_csv and os.path.exists(disc_csv):
            return {**base, 'stage': 'complete'}

    return {**base, 'stage': 'disc_running', 'disc_task_id': disc_status.get('task_id', disc_task_id)}


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

    try:
        actives_path = str(handle_file_upload_secure(actives_file, disc_job_id, "actives_"))
        decoys_path  = str(handle_file_upload_secure(decoys_file, disc_job_id, "decoys_"))
    except SecurityError as e:
        st.error(f"File validation failed: {e}")
        st.stop()

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

    # Persist disc IDs into pipeline status file so resume can find them
    pipeline_status_file = os.path.join(RESULTS_DIR, f"{pipeline_job_id}_status.json")
    if os.path.exists(pipeline_status_file):
        try:
            with open(pipeline_status_file) as f:
                pipeline_status = json.load(f)
            pipeline_status['disc_job_id']  = disc_job_id
            pipeline_status['disc_task_id'] = task.id
            with open(pipeline_status_file, 'w') as f:
                json.dump(pipeline_status, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not write disc IDs to pipeline status: {e}")

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


# ── Step 4: Results ──────────────────────────────────────────────────────────

def _show_molecule_3d_with_pocket(pdb_path: str, pocket_residues: list,
                                   width: int = 400, height: int = 380) -> None:
    """Render a py3Dmol viewer with pocket residues highlighted in orange."""
    try:
        with open(pdb_path, 'r') as f:
            pdb_data = f.read()
        specs = []
        for res_str in pocket_residues:
            parts = res_str.strip().split('_', 1)
            if len(parts) == 2:
                try:
                    specs.append({'chain': parts[0], 'resi': int(parts[1])})
                except ValueError:
                    pass
        view = py3Dmol.view(width=width, height=height)
        view.addModel(pdb_data, 'pdb')
        view.setStyle({}, {'cartoon': {'color': 'spectrum'}})
        for s in specs:
            view.setStyle({'chain': s['chain'], 'resi': s['resi']},
                          {'stick': {'color': 'orange', 'radius': 0.3}})
        if specs:
            chains = {}
            for s in specs:
                chains.setdefault(s['chain'], []).append(s['resi'])
            for chain, resis in chains.items():
                view.addSurface(py3Dmol.VDW, {'opacity': 0.4, 'color': 'orange'},
                                {'chain': chain, 'resi': resis})
            view.zoomTo({'resi': [s['resi'] for s in specs]})
        else:
            view.zoomTo()
        view.spin(False)
        html = f'<div style="border-radius:10px;overflow:hidden;">{view._make_html()}</div>'
        components.html(html, height=height + 40, scrolling=False)
    except Exception as e:
        st.warning(f"Could not load 3D structure: {e}")


@st.cache_data(ttl=300)
def _load_disc_results(results_csv: str) -> pd.DataFrame:
    return pd.read_csv(results_csv)


def _render_results(pipeline_job_id: str, disc_job_id: str) -> None:
    """Render Step 4: ranked table, ROC chart, 3D viewer, download."""

    # Locate results CSV from discrimination status file
    status_file = os.path.join(RESULTS_DIR, disc_job_id + '_status.json')
    results_csv = None
    if os.path.exists(status_file):
        with open(status_file) as f:
            status_data = json.load(f)
        results_csv = status_data.get('result_info', {}).get('discrimination_results_csv')

    # Fallback: look up via Celery result
    if not results_csv:
        disc_task_id = st.session_state.wiz_disc_task_id
        if disc_task_id:
            res = celery_app.AsyncResult(disc_task_id)
            if res.state == 'SUCCESS':
                results_csv = (res.result or {}).get('discrimination_results_csv')

    if not results_csv or not os.path.exists(results_csv):
        st.warning("Results file not found. The discrimination job may still be finishing.")
        return

    _results_dir_real = os.path.realpath(RESULTS_DIR) + os.sep
    if not os.path.realpath(results_csv).startswith(_results_dir_real):
        st.error("Invalid results path.")
        return

    df = _load_disc_results(results_csv)

    if df.empty:
        st.warning("Discrimination results are empty.")
        return

    # ── Success banner ──
    st.markdown(
        f'<div class="ph-success">'
        f'<div style="font-size:14px;font-weight:600;color:#00a085;">Analysis complete</div>'
        f'<div style="font-size:12px;color:#888;margin-top:2px;">'
        f'{len(df)} conformations ranked by discrimination score'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    # ── Results panel ──
    _panel_open("ph-panel-done")
    _panel_header("Results — Ranked Conformations", "Done", "done")
    st.markdown('<div class="ph-panel-body">', unsafe_allow_html=True)

    # Table with colour coding
    display_cols = [c for c in ['cluster_id', 'frame', 'roc_auc', 'ef1', 'ef5'] if c in df.columns]

    def _colour_auc(val):
        if val >= 0.7:
            return 'background-color: rgba(0,168,133,.12)'
        if val >= 0.6:
            return 'background-color: rgba(192,134,58,.12)'
        return ''

    st.dataframe(
        df[display_cols].style.map(_colour_auc, subset=['roc_auc'] if 'roc_auc' in display_cols else []),
        use_container_width=True,
        hide_index=True,
    )

    # ROC-AUC bar chart
    if 'roc_auc' in df.columns:
        labels = [
            f"Cluster {row['cluster_id']} (Fr.{row['frame']})"
            if 'cluster_id' in df.columns and 'frame' in df.columns
            else f"#{i+1}"
            for i, (_, row) in enumerate(df.iterrows())
        ]
        colors = [
            '#6B7FA8' if v >= 0.7 else '#c0863a' if v >= 0.6 else '#d63031'
            for v in df['roc_auc']
        ]
        fig = go.Figure(go.Bar(x=labels, y=df['roc_auc'], marker_color=colors))
        fig.update_layout(
            xaxis_title="Conformation",
            yaxis_title="ROC-AUC",
            yaxis=dict(range=[0, 1]),
            height=300,
            margin=dict(t=20, b=60),
            plot_bgcolor='rgba(0,0,0,0)',
            paper_bgcolor='rgba(0,0,0,0)',
        )
        fig.add_hline(y=0.5, line_dash='dash', line_color='#ccc', annotation_text='Random baseline')
        st.plotly_chart(fig, use_container_width=True)

    # 3D viewer for top-ranked cluster
    if len(df) > 0 and 'frame' in df.columns:
        top_row = df.iloc[0]
        cluster_job_id = (st.session_state.wiz_pipeline_result or {}).get(
            'cluster_job_id', pipeline_job_id
        )
        reps_csv_path = os.path.join(
            RESULTS_DIR, cluster_job_id, 'pocket_clusters', 'cluster_representatives.csv'
        )
        if os.path.exists(reps_csv_path):
            reps_df = pd.read_csv(reps_csv_path)
            top_cluster_id = top_row.get('cluster_id')
            subset = reps_df[reps_df['cluster'] == top_cluster_id] if top_cluster_id is not None else reps_df
            if subset.empty:
                st.caption("Representative structure not found in cluster results.")
            else:
                top_rep = subset.iloc[0]

                pdb_name = str(top_rep['File name'])
                if '_predictions' in pdb_name:
                    pdb_name = pdb_name.replace('_predictions', '')
                if not pdb_name.endswith('.pdb'):
                    pdb_name += '.pdb'
                pdb_path = os.path.join(RESULTS_DIR, pipeline_job_id, 'pdbs', pdb_name)

                residues_raw = str(top_rep.get('residues', ''))
                residues = [r.strip() for r in residues_raw.replace(',', ' ').split() if r.strip()]

                st.markdown(
                    f'<div style="font-size:12px;font-weight:600;color:#2a3a4a;margin:14px 0 6px;">'
                    f'Top-ranked structure — Cluster {top_cluster_id}</div>',
                    unsafe_allow_html=True,
                )
                if os.path.exists(pdb_path):
                    _show_molecule_3d_with_pocket(pdb_path, residues)
                else:
                    st.caption(f"PDB not found: {pdb_path}")

    st.markdown('</div></div>', unsafe_allow_html=True)

    # ── Download ──
    n_top = st.slider("Top conformations to include in ZIP", 1, min(5, len(df)), min(3, len(df)), key="wiz_n_top")
    if st.button("Prepare download ZIP", key="wiz_prep_zip"):
        top_df    = df.head(n_top)
        disc_dir  = os.path.join(RESULTS_DIR, disc_job_id, 'discrimination')
        os.makedirs(disc_dir, exist_ok=True)
        zip_path  = os.path.join(disc_dir, 'top_conformations.zip')
        pdb_dir   = os.path.join(RESULTS_DIR, pipeline_job_id, 'pdbs')

        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.write(results_csv, 'discrimination_results.csv')
            for _, row in top_df.iterrows():
                frame = str(row.get('frame', ''))
                if pdb_dir and os.path.isdir(pdb_dir):
                    matches = [f for f in os.listdir(pdb_dir) if f'_{frame}.pdb' in f]
                    for pdb_file in matches:
                        zf.write(
                            os.path.join(pdb_dir, pdb_file),
                            f"top_conformations/cluster_{row.get('cluster_id', 'x')}_{pdb_file}",
                        )
        with open(zip_path, 'rb') as f:
            st.download_button(
                "Download ZIP",
                data=f.read(),
                file_name=f"top_conformations_{disc_job_id}.zip",
                mime="application/zip",
                key="wiz_dl_zip",
            )

    # ── New analysis ──
    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("Start new analysis", key="wiz_new"):
        for k in ('wiz_stage', 'wiz_job_id', 'wiz_task_id', 'wiz_pipeline_result',
                  'wiz_disc_job_id', 'wiz_disc_task_id'):
            if k == 'wiz_stage':
                st.session_state[k] = 'setup'
            else:
                st.session_state[k] = None
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

elif stage == 'complete':
    _render_step_strip(4)
    _render_job_banner(st.session_state.wiz_job_id, show_warn=False)
    _render_done_panel_step1()
    _render_pipeline_done_panel(st.session_state.wiz_pipeline_result or {})
    _render_results(st.session_state.wiz_job_id, st.session_state.wiz_disc_job_id)

elif stage == 'error':
    st.error("A task failed. Check the error above or start a new analysis.")
    if st.button("Start new analysis", key="wiz_new_from_error"):
        for k in ('wiz_stage','wiz_job_id','wiz_task_id','wiz_pipeline_result',
                  'wiz_disc_job_id','wiz_disc_task_id'):
            st.session_state[k] = 'setup' if k == 'wiz_stage' else None
        st.rerun()
