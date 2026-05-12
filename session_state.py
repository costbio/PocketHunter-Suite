"""
Centralized session state initialization for PocketHunter Suite.

Conceptual model — three distinct concerns, one source of truth each:

1. **Active jobs** (per-stage): ``find_pockets_job_id``, ``cluster_job_id``,
   ``docking_job_id``, ``pipeline_job_id`` plus their ``*_task_id`` and
   ``*_status`` partners. The ``cached_job_ids`` dict mirrors these so other
   pages can pre-fill the Job ID inputs.

   Legacy keys ``extract_job_id`` and ``detect_job_id`` remain in the dict
   for backward compat (older status files + the Task Monitor surface them)
   but no live page writes to them after the Step 1+2 merge.

2. **Docking target selection** — which clusters / PDBs the user wants
   to dock against. Single canonical key: ``docking_target_clusters``
   (the list of cluster IDs the user picked, from the heatmap or
   elsewhere). Per-PDB checkboxes use stable keys keyed off
   Frame_pocket_index (see ``get_pdb_selection_key``).

3. **Preview state** — what the user has currently expanded in the
   3D viewer on Step 3 / Pipeline. Single canonical group:
   ``cluster_preview_id`` / ``cluster_preview_pdb`` /
   ``cluster_preview_residues``. Strictly UI-local; not consumed by
   any Celery task.

Renames from earlier iterations:
- ``heatmap_docking_clusters`` → ``docking_target_clusters``
- ``heatmap_selected_cluster_id`` → ``cluster_preview_id``
- ``heatmap_selected_pdb_path`` → ``cluster_preview_pdb``
- ``heatmap_selected_residues`` → ``cluster_preview_residues``

The unused ``view_mode`` key has been removed (was never read).
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
            'find_pockets': None,  # merged Step 1+2 (canonical post-merge)
            'extract': None,       # legacy — preserved for old status files
            'detect': None,        # legacy — preserved for old status files
            'cluster': None,
            'docking': None,
            'pipeline': None,
        }

    # Find Pockets state (merged Step 1+2)
    if 'find_pockets_job_id' not in st.session_state:
        st.session_state.find_pockets_job_id = None
    if 'find_pockets_task_id' not in st.session_state:
        st.session_state.find_pockets_task_id = None
    if 'find_pockets_status' not in st.session_state:
        st.session_state.find_pockets_status = 'idle'

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

    # Docking state
    if 'docking_job_id' not in st.session_state:
        st.session_state.docking_job_id = None
    if 'docking_task_id' not in st.session_state:
        st.session_state.docking_task_id = None
    if 'docking_display_job_id' not in st.session_state:
        st.session_state.docking_display_job_id = None

    # Docking PDB selections - stores selected PDB files by Frame_pocket_index
    if 'docking_selected_pdbs' not in st.session_state:
        st.session_state.docking_selected_pdbs = {}

    # Heatmap job tracking - used to detect job changes and clear stale state
    if 'heatmap_last_job_id' not in st.session_state:
        st.session_state.heatmap_last_job_id = None

    # Pipeline inline docking state
    if 'pipe_docking_task_id' not in st.session_state:
        st.session_state.pipe_docking_task_id = None
    if 'pipe_docking_job_id' not in st.session_state:
        st.session_state.pipe_docking_job_id = None
    if 'pipe_selected_pose' not in st.session_state:
        st.session_state.pipe_selected_pose = None

    # Docking target selection — which clusters the user picked to dock against.
    # Populated by the cluster-page heatmap or the pipeline inline heatmap.
    if 'docking_target_clusters' not in st.session_state:
        st.session_state.docking_target_clusters = []

    # Cluster preview state — what the Step 3 / pipeline 3D viewer is showing.
    # Strictly UI-local: not consumed by any Celery task.
    if 'cluster_preview_id' not in st.session_state:
        st.session_state.cluster_preview_id = None
    if 'cluster_preview_pdb' not in st.session_state:
        st.session_state.cluster_preview_pdb = None
    if 'cluster_preview_residues' not in st.session_state:
        st.session_state.cluster_preview_residues = []

    # 3D Viewer state
    if 'selected_pocket' not in st.session_state:
        st.session_state.selected_pocket = None
    if 'selected_pose' not in st.session_state:
        st.session_state.selected_pose = None


def get_pdb_selection_key(filename: str, row_index=None, row_id=None) -> str:
    """Stable per-row session_state key for a PDB selection checkbox.

    Prefers ``row_id`` (the ``Frame_pocket_index`` from the
    cluster_representatives.csv — a unique row identifier that survives
    DataFrame resorts). Falls back to a sanitized ``filename`` + ``row_index``
    when ``row_id`` is None/empty, preserving backward compatibility with
    older CSVs that don't carry Frame_pocket_index.
    """
    if row_id is not None and str(row_id).strip():
        safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in str(row_id))
        return f"pdb_select_{safe}"
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


def render_load_previous_widget(
    *,
    session_key: str,
    label: str = "Load previous results",
    placeholder: str = "Enter a job ID",
    text_input_key: str | None = None,
    button_key: str | None = None,
    on_load=None,
) -> None:
    """Shared "load a previous job by ID" expander used on every step page.

    Validates the entered ID via ``FileValidator.validate_job_id`` before
    writing it to ``st.session_state[session_key]``. On a valid load, runs
    ``on_load(validated_id)`` (if provided) and then ``st.rerun()``. On
    validation failure, surfaces ``st.error(...)`` and leaves the page
    rendered so the user can correct the input.
    """
    from security import FileValidator, SecurityError

    text_input_key = text_input_key or f"_load_prev_input_{session_key}"
    button_key = button_key or f"_load_prev_btn_{session_key}"

    with st.expander(label):
        load_id = st.text_input(
            "Job ID:",
            value="",
            placeholder=placeholder,
            key=text_input_key,
        )
        if st.button("Load Results", key=button_key):
            if not load_id:
                st.error("Enter a job ID first.")
                return
            try:
                safe_id = FileValidator.validate_job_id(load_id.strip())
            except SecurityError as e:
                st.error(f"Invalid job ID: {e}")
                return
            st.session_state[session_key] = safe_id
            if on_load is not None:
                on_load(safe_id)
            st.rerun()
