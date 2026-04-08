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
    st.markdown(f'<div class="ph-panel {extra_class}">', unsafe_allow_html=True)


def _panel_close() -> None:
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
    st.markdown('<div class="ph-panel-body">', unsafe_allow_html=True)

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

    st.markdown('</div></div>', unsafe_allow_html=True)  # close panel-body + panel

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
    job_upload_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_upload_dir, exist_ok=True)

    traj_path = os.path.join(job_upload_dir, traj_file.name)
    topo_path = os.path.join(job_upload_dir, topo_file.name)
    with open(traj_path, 'wb') as f:
        f.write(traj_file.getbuffer())
    with open(topo_path, 'wb') as f:
        f.write(topo_file.getbuffer())

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


# ── Page render ──────────────────────────────────────────────────────────────

stage = st.session_state.wiz_stage

if stage == 'setup':
    _render_resume_bar()
    _render_step_strip(1)
    _render_step1_form()
    _render_locked_panel("Step 2 — Extract · Detect · Cluster")
    _render_locked_panel("Step 3 — Discrimination Analysis")

else:
    st.info("Pipeline stages (Tasks 4-7) — coming in next tasks.")
