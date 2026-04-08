"""
Centralized session state initialization for PocketHunter Suite.

This module provides consistent session state initialization across all app modules
to prevent key collisions and ensure proper defaults.
"""

import streamlit as st


def initialize_session_state():
    """
    Initialize all session state variables with proper defaults.
    Call this at the start of each page to ensure consistency.
    """
    # Job ID caching - used to track jobs across steps
    if 'cached_job_ids' not in st.session_state:
        st.session_state.cached_job_ids = {
            'extract': None,
            'detect': None,
            'cluster': None,
            'discrimination': None,
            'pipeline': None,
        }

    # Extract Frames state
    if 'extract_job_id' not in st.session_state:
        st.session_state.extract_job_id = None
    if 'extract_task_id' not in st.session_state:
        st.session_state.extract_task_id = None
    if 'extract_status' not in st.session_state:
        st.session_state.extract_status = 'idle'

    # Detect Pockets state
    if 'detect_job_id' not in st.session_state:
        st.session_state.detect_job_id = None
    if 'detect_task_id' not in st.session_state:
        st.session_state.detect_task_id = None
    if 'detect_status' not in st.session_state:
        st.session_state.detect_status = 'idle'

    # Cluster Pockets state
    if 'cluster_job_id' not in st.session_state:
        st.session_state.cluster_job_id = None
    if 'cluster_task_id' not in st.session_state:
        st.session_state.cluster_task_id = None
    if 'cluster_status' not in st.session_state:
        st.session_state.cluster_status = 'idle'

    # Full Pipeline state
    if 'pipeline_job_id' not in st.session_state:
        st.session_state.pipeline_job_id = None
    if 'pipeline_task_id' not in st.session_state:
        st.session_state.pipeline_task_id = None
    if 'pipeline_status' not in st.session_state:
        st.session_state.pipeline_status = 'idle'

    # Heatmap interactive selection state
    if 'heatmap_selected_cluster_id' not in st.session_state:
        st.session_state.heatmap_selected_cluster_id = None
    if 'heatmap_selected_pdb_path' not in st.session_state:
        st.session_state.heatmap_selected_pdb_path = None
    if 'heatmap_selected_residues' not in st.session_state:
        st.session_state.heatmap_selected_residues = []

    # Heatmap job tracking
    if 'heatmap_last_job_id' not in st.session_state:
        st.session_state.heatmap_last_job_id = None

    # Discrimination state
    if 'discrimination_job_id' not in st.session_state:
        st.session_state.discrimination_job_id = None
    if 'discrimination_task_id' not in st.session_state:
        st.session_state.discrimination_task_id = None
    if 'discrimination_status' not in st.session_state:
        st.session_state.discrimination_status = 'idle'

    # 3D Viewer state
    if 'selected_pocket' not in st.session_state:
        st.session_state.selected_pocket = None
    if 'selected_pose' not in st.session_state:
        st.session_state.selected_pose = None

    # Wizard state
    if 'wiz_stage' not in st.session_state:
        # Stages: 'setup' | 'pipeline_running' | 'pipeline_done' | 'disc_ready'
        #         | 'disc_running' | 'complete' | 'error'
        st.session_state.wiz_stage = 'setup'
    if 'wiz_job_id' not in st.session_state:
        st.session_state.wiz_job_id = None        # pipeline job ID
    if 'wiz_task_id' not in st.session_state:
        st.session_state.wiz_task_id = None       # pipeline Celery task ID
    if 'wiz_pipeline_result' not in st.session_state:
        st.session_state.wiz_pipeline_result = None  # dict from successful pipeline
    if 'wiz_disc_job_id' not in st.session_state:
        st.session_state.wiz_disc_job_id = None   # discrimination job ID
    if 'wiz_disc_task_id' not in st.session_state:
        st.session_state.wiz_disc_task_id = None  # discrimination Celery task ID


def get_pdb_selection_key(filename: str, row_index=None) -> str:
    """
    Generate a unique session state key for PDB file selection.

    Args:
        filename: The PDB filename
        row_index: Optional DataFrame row index to disambiguate duplicate filenames

    Returns:
        A unique key string for session state
    """
    # Sanitize filename to create valid key
    safe_name = filename.replace('.', '_').replace(' ', '_').replace('-', '_')
    if row_index is not None:
        return f"pdb_select_{safe_name}_{row_index}"
    return f"pdb_select_{safe_name}"


