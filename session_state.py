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
            'docking': None
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

    # Docking state
    if 'docking_job_id' not in st.session_state:
        st.session_state.docking_job_id = None
    if 'docking_task_id' not in st.session_state:
        st.session_state.docking_task_id = None
    if 'docking_display_job_id' not in st.session_state:
        st.session_state.docking_display_job_id = None
    if 'view_mode' not in st.session_state:
        st.session_state.view_mode = 'setup'

    # Docking PDB selections - stores selected PDB files by filename
    if 'docking_selected_pdbs' not in st.session_state:
        st.session_state.docking_selected_pdbs = {}

    # 3D Viewer state
    if 'selected_pocket' not in st.session_state:
        st.session_state.selected_pocket = None
    if 'selected_pose' not in st.session_state:
        st.session_state.selected_pose = None


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


def clear_docking_selections():
    """Clear all PDB selections for docking."""
    st.session_state.docking_selected_pdbs = {}
    # Also clear any legacy pdb_{idx} keys
    keys_to_remove = [k for k in st.session_state.keys() if k.startswith('pdb_')]
    for key in keys_to_remove:
        del st.session_state[key]
