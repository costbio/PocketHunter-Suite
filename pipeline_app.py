import streamlit as st
import os
import zipfile
import uuid
import json
from datetime import datetime
import time
import math
import re
from pathlib import Path
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
import py3Dmol
import streamlit.components.v1 as components
from tasks import run_pockethunter_pipeline, run_docking_task
from celery_app import celery_app
from config import Config
from security import handle_file_upload_secure, SecurityError
from rate_limiter import RateLimitExceeded, check_task_rate_limit
from logging_config import setup_logging
from session_state import initialize_session_state
from cluster_labels import describe_cluster_spatially

UPLOAD_DIR = str(Config.UPLOAD_DIR)
RESULTS_DIR = str(Config.RESULTS_DIR)

logger = setup_logging(__name__)

# Brutalist progress-strip CSS — only the .stage-* classes survive from the
# legacy gradient styling. Page header below uses the global .bh* pattern.
st.markdown("""
<style>
    .stage-indicator {
        display: flex;
        justify-content: space-between;
        margin: 1rem 0 1.5rem 0;
        gap: 4px;
    }
    .stage-box {
        flex: 1;
        text-align: center;
        padding: 8px 12px;
        font-family: 'JetBrains Mono', ui-monospace, monospace;
        font-size: 0.78rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        border: 2px solid #000;
    }
    .stage-done    { background: #fff; color: #000; }
    .stage-active  { background: #d4ff00; color: #000; }
    .stage-pending { background: #f0f0f0; color: #666; }
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="bh" style="margin-top: 4px;">
    <div class="bh-row">
        <span class="bh-title">Full Pipeline</span>
        <span class="bh-version">[MD → DOCK]</span>
    </div>
    <div class="bh-rule"></div>
    <div class="bh-stages">
        Extract <span class="sep">►</span> Detect <span class="sep">►</span>
        Cluster <span class="sep">►</span> optional Dock
    </div>
</div>
""", unsafe_allow_html=True)

# ── Cluster results helpers ─────────────────────────────────────────────

def _resolve_pdb_path(file_name, job_id):
    pdb_name = file_name.replace('_predictions', '') if '_predictions' in file_name else file_name
    if not pdb_name.endswith('.pdb'):
        pdb_name += '.pdb'
    candidate = os.path.join(RESULTS_DIR, job_id, 'pdbs', pdb_name)
    if os.path.exists(candidate):
        return candidate
    candidate2 = os.path.join(RESULTS_DIR, job_id, 'pocket_clusters', pdb_name)
    if os.path.exists(candidate2):
        return candidate2
    return candidate


@st.cache_data(ttl=300)
def _load_clustered_data(path):
    return pd.read_csv(path)


@st.cache_data(ttl=300)
def _load_representatives(path):
    return pd.read_csv(path)


# The docking 3D viewer + affinity classifier live in docking_visualization.
# The auto-box helper lives in docking_selection. Aliases kept for in-file
# callers that already reference the old _underscore names.
from docking_visualization import (
    classify_affinity as _classify_affinity,
    extract_sdf_model as _extract_sdf_model,
    show_molecule_3d as _show_docking_molecule_3d,
)
from docking_selection import auto_box_for_selection as _compute_inline_auto_box_raw


def _compute_inline_auto_box(df_reps, results_job_id, selected_clusters):
    """Thin wrapper preserving the original (df_reps, job_id, selected) call shape."""
    return _compute_inline_auto_box_raw(df_reps, results_job_id, selected_clusters)


def _show_pipeline_cluster_inline(results_job_id):
    """Render the heatmap + 3D viewer + docking launch inline on the pipeline page."""
    cluster_output_dir = os.path.join(RESULTS_DIR, results_job_id, "pocket_clusters")
    representatives_file = os.path.join(cluster_output_dir, "cluster_representatives.csv")

    if not os.path.exists(representatives_file):
        st.info("Cluster results not yet available.")
        return

    # Clear heatmap state when job changes
    if st.session_state.get('heatmap_last_job_id') != results_job_id:
        st.session_state.cluster_preview_id = None
        st.session_state.cluster_preview_pdb = None
        st.session_state.cluster_preview_residues = []
        st.session_state.docking_target_clusters = []
        st.session_state.heatmap_last_job_id = results_job_id

    try:
        df_reps = _load_representatives(representatives_file)
        if 'residues' in df_reps.columns and df_reps['residues'].dtype == object:
            df_reps['num_residues'] = df_reps['residues'].apply(
                lambda x: len(str(x).split()) if pd.notna(x) else 0
            )
        else:
            df_reps['num_residues'] = df_reps.get('residues', 0)

        if len(df_reps) == 0:
            st.warning("⚠️ Clustering completed but no representative pockets found.")
            return

        st.markdown("---")
        st.markdown("### 🗺️ Clustering Results")

        with st.expander("ℹ️ Reading these results — what's a 'representative'?"):
            st.markdown(
                """
Each cluster groups pockets that touch a similar set of residues across
trajectory frames. The **representative** is the *single PDB frame* whose
pocket is closest (by Hamming distance on the residue-presence vector) to all
other pockets in that cluster — i.e. the most typical member, **not an
average structure**.

- The 3D Viewer and the docking step operate on this one representative frame.
- The **Residue Heatmap** shows residue *frequency across all pockets in the
  cluster*, so the representative's exact residues may not match the brightest
  spots on the heatmap.
- The full footprint of a cluster (the union of residues across every member
  pocket) can be larger than what the representative alone shows.
                """.strip()
            )

        clustered_file = os.path.join(cluster_output_dir, "pockets_clustered.csv")
        if not os.path.exists(clustered_file):
            st.info("Heatmap requires pockets_clustered.csv — not found for this job.")
            return

        df_clustered = _load_clustered_data(clustered_file)
        df_clustered = df_clustered[df_clustered['cluster'] != -1]

        # Shared 3-column consensus panel (heatmap + checkboxes + 3D viewer).
        # Same call site as cluster_pockets_app — only the key_prefix differs
        # so the two pages' widget state doesn't collide.
        from cluster_visualization import render_consensus_panel
        render_consensus_panel(
            df_clustered, df_reps, results_job_id,
            key_prefix="pipe_cluster",
            viewer_size=(400, 420),
        )

        # ── Inline Docking Section ──────────────────────────────────────────
        st.markdown("---")
        st.markdown("### 🧪 Molecular Docking")

        pipe_task_id = st.session_state.get('pipe_docking_task_id')
        pipe_job_id = st.session_state.get('pipe_docking_job_id')

        if pipe_task_id:
            dock_task = celery_app.AsyncResult(pipe_task_id)

            if dock_task.state == 'PENDING':
                st.info("⏳ Docking queued…")
                st.progress(0)
                time.sleep(3)
                st.rerun()

            elif dock_task.state == 'PROGRESS':
                info = dock_task.info or {}
                progress = info.get('progress', 0)
                current_step = info.get('current_step', 'Processing…')
                pairs_done = info.get('pairs_done', 0)
                pairs_total = info.get('pairs_total', 0)
                st.progress(progress / 100, text=f"{progress}% — {current_step}")
                if pairs_total > 0:
                    st.progress(pairs_done / pairs_total,
                                text=f"{pairs_done}/{pairs_total} receptor–ligand pairs")
                if progress <= 15:
                    st.caption("⏱️ Preparing receptors (converting PDB → PDBQT)…")
                time.sleep(3)
                st.rerun()

            elif dock_task.state == 'SUCCESS':
                results = dock_task.result or {}
                st.success("✅ Docking completed!")

                # Partial-success callout: surface per-pair failures (if any)
                from failure_view import render_pair_failures_callout
                render_pair_failures_callout(results, st.session_state.get('pipe_docking_job_id'))

                best_aff = results.get('best_affinity', 0)
                _, aff_emoji = _classify_affinity(best_aff)
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Total Poses", results.get('total_docking_poses', 0))
                col2.metric("Unique Ligands", results.get('unique_ligands', 0))
                col3.metric("Unique Receptors", results.get('unique_receptors', 0))
                col4.metric(f"Best Affinity {aff_emoji}", f"{best_aff:.2f} kcal/mol")

                results_file = results.get('docking_results_file')
                if results_file and os.path.exists(results_file):
                    df_dock = pd.read_csv(results_file)
                    if not df_dock.empty and 'ligand' in df_dock.columns and 'receptor' in df_dock.columns:
                        df_best_dock = df_dock.loc[
                            df_dock.groupby(['ligand', 'receptor'])['affinity (kcal/mol)'].idxmin()
                        ].sort_values('affinity (kcal/mol)')
                        df_best_dock['Quality'] = df_best_dock['affinity (kcal/mol)'].apply(
                            lambda x: f"{_classify_affinity(x)[1]} {_classify_affinity(x)[0]}"
                        )

                        # ── Split view: table + 3D viewer ──────────────────
                        st.markdown("#### 🎯 Results Explorer with 3D Visualization")

                        filt_col1, filt_col2 = st.columns([2, 1])
                        with filt_col1:
                            affinity_filter = st.multiselect(
                                "Filter by Affinity:",
                                options=['excellent', 'good', 'moderate', 'poor'],
                                default=['excellent', 'good'],
                                key="pipe_affinity_filter",
                            )
                        with filt_col2:
                            top_n = st.slider("Show top N:", 5, 50, 15, key="pipe_top_n")

                        df_filtered = df_best_dock[
                            df_best_dock['affinity (kcal/mol)'].apply(
                                lambda x: _classify_affinity(x)[0]
                            ).isin(affinity_filter)
                        ] if affinity_filter else df_best_dock
                        df_display = df_filtered.head(top_n)

                        table_col, viewer_col = st.columns([1, 1])

                        with table_col:
                            st.markdown("##### 🏆 Top Poses")
                            if not df_display.empty:
                                selected_idx = st.selectbox(
                                    "Select pose to view:",
                                    df_display.index.tolist(),
                                    format_func=lambda x: (
                                        f"{df_display.loc[x, 'Quality'].split()[0]} "
                                        f"{df_display.loc[x, 'ligand']} ↔ "
                                        f"{df_display.loc[x, 'receptor']} "
                                        f"({df_display.loc[x, 'affinity (kcal/mol)']:.2f} kcal/mol)"
                                    ),
                                    key="pipe_pose_selector",
                                )
                                if selected_idx is not None:
                                    st.session_state.pipe_selected_pose = df_display.loc[selected_idx].to_dict()
                                st.dataframe(
                                    df_display[['ligand', 'receptor', 'affinity (kcal/mol)',
                                                'Quality', 'rmsd l.b.', 'rmsd u.b.']].rename(columns={
                                        'affinity (kcal/mol)': 'Affinity', 'Quality': '🎯',
                                        'rmsd l.b.': 'RMSD LB', 'rmsd u.b.': 'RMSD UB',
                                    }),
                                    use_container_width=True, height=300,
                                )
                            else:
                                st.info("No poses match the selected filters.")

                        with viewer_col:
                            st.markdown("##### 🔬 3D Structure")
                            pose = st.session_state.get('pipe_selected_pose')
                            if pose:
                                cat, emoji = _classify_affinity(pose.get('affinity (kcal/mol)', 0))
                                st.markdown(
                                    f'<div style="background:linear-gradient(135deg,#667eea,#764ba2);'
                                    f'padding:0.7rem 1rem;border-radius:8px;color:white;margin-bottom:0.5rem;">'
                                    f'<strong>{emoji} {pose.get("ligand","?")} ↔ {pose.get("receptor","?")}</strong>'
                                    f'<br><span style="font-size:1.1rem;font-weight:bold;">'
                                    f'{pose.get("affinity (kcal/mol)",0):.2f} kcal/mol</span>'
                                    f'</div>',
                                    unsafe_allow_html=True,
                                )
                                viz_style = st.selectbox(
                                    "Style:", ["cartoon", "surface", "stick", "binding site"],
                                    key="pipe_viz_style",
                                )
                                show_ligand = st.checkbox("Show Ligand", value=True, key="pipe_show_lig")

                                ligand_sdf_data = None
                                if show_ligand:
                                    sdf_path = pose.get('output_sdf')
                                    mode = pose.get('mode')
                                    if sdf_path and mode is not None:
                                        ligand_sdf_data = _extract_sdf_model(sdf_path, mode)

                                receptor_data = None
                                for path_key in ('receptor_pdb_path', 'receptor_path'):
                                    rpath = pose.get(path_key)
                                    if rpath and os.path.exists(rpath):
                                        with open(rpath, 'r') as f:
                                            receptor_data = f.read()
                                        break
                                if receptor_data is None:
                                    docking_dir = results.get('docking_output_dir')
                                    if docking_dir:
                                        rec_name = pose.get('receptor', '')
                                        for candidate in [
                                            os.path.join(docking_dir, rec_name),
                                            os.path.join(docking_dir, rec_name + '.pdb'),
                                            os.path.join(docking_dir, rec_name + '.pdbqt'),
                                        ]:
                                            if os.path.exists(candidate):
                                                with open(candidate, 'r') as f:
                                                    receptor_data = f.read()
                                                break

                                if receptor_data:
                                    _show_docking_molecule_3d(
                                        receptor_data, ligand_sdf_data,
                                        width=420, height=380,
                                        style_protein=viz_style,
                                    )
                                else:
                                    st.warning("⚠️ Receptor structure file not found.")
                            else:
                                st.info("← Select a pose from the table to view it here.")

                        # Bar chart + download below
                        fig_bar = px.bar(
                            df_best_dock.head(20),
                            x='ligand', y='affinity (kcal/mol)', color='receptor',
                            title='Best Affinity per Ligand (lower = stronger binding)',
                            labels={'affinity (kcal/mol)': 'Affinity (kcal/mol)'},
                        )
                        fig_bar.update_layout(height=300)
                        st.plotly_chart(fig_bar, use_container_width=True, key="pipe_dock_bar")

                        st.download_button(
                            "📥 Download Full Results (CSV)",
                            data=df_dock.to_csv(index=False),
                            file_name=f"docking_results_{pipe_job_id}.csv",
                            mime="text/csv",
                        )
                    else:
                        st.warning("⚠️ Results file is empty or missing required columns.")
                else:
                    st.warning("No results file found.")

                if st.button("🔄 Re-run Docking with Different Ligands", key="pipe_redock"):
                    st.session_state.pipe_docking_task_id = None
                    st.session_state.pipe_docking_job_id = None
                    st.rerun()

            elif dock_task.state == 'FAILURE':
                st.error(f"❌ Docking failed: {dock_task.info}")
                if st.button("🔄 Retry Docking", key="pipe_retry_docking"):
                    st.session_state.pipe_docking_task_id = None
                    st.session_state.pipe_docking_job_id = None
                    st.rerun()

        else:
            # ── Docking input form (shown when no task is running) ──────────
            selected_clusters = st.session_state.docking_target_clusters
            if selected_clusters:
                selected_str = ", ".join(str(c) for c in selected_clusters)
                st.info(f"Clusters selected for docking: **{selected_str}**")

                st.markdown("#### 📁 Ligand Files")
                ligand_files = st.file_uploader(
                    f"Upload ligand files (PDBQT or ZIP, max {Config.MAX_DOCKING_LIGANDS})",
                    type=['pdbqt', 'zip'],
                    accept_multiple_files=True,
                    key="pipe_dock_ligands",
                )

                st.markdown("#### ⚙️ Docking Parameters")
                dc1, dc2, dc3 = st.columns(3)
                with dc1:
                    d_poses = st.slider("Poses per Ligand", 1, 20, 10, key="pipe_dock_poses")
                with dc2:
                    d_exhaust = st.slider(
                        "Exhaustiveness", 1, Config.MAX_DOCKING_EXHAUSTIVENESS,
                        min(8, Config.MAX_DOCKING_EXHAUSTIVENESS), key="pipe_dock_exhaust",
                    )
                with dc3:
                    d_ph = st.slider("pH", 4.0, 10.0, 7.4, step=0.1, key="pipe_dock_ph")

                # Compute auto-sized box from the highest-probability selected
                # cluster's representative. Best-effort; falls back to 20Å on
                # any failure.
                _pipe_auto_box = _compute_inline_auto_box(
                    df_reps, results_job_id, selected_clusters
                )
                if _pipe_auto_box:
                    _ax, _ay, _az, _alab = _pipe_auto_box
                    if st.button(
                        f"🎯 Auto-size box from {_alab}",
                        use_container_width=True,
                        key="pipe_auto_box_btn",
                        help=(
                            f"Suggested: X={_ax}, Y={_ay}, Z={_az} Å — top-probability "
                            "rep + 4 Å padding. Values are clamped to 10–50 Å."
                        ),
                    ):
                        st.session_state['pipe_dock_box_x'] = float(min(max(_ax, 10.0), 50.0))
                        st.session_state['pipe_dock_box_y'] = float(min(max(_ay, 10.0), 50.0))
                        st.session_state['pipe_dock_box_z'] = float(min(max(_az, 10.0), 50.0))
                        st.rerun()
                bx1, bx2, bx3 = st.columns(3)
                with bx1:
                    d_box_x = st.slider("Box X (Å)", 10.0, 50.0, 20.0, step=1.0, key="pipe_dock_box_x")
                with bx2:
                    d_box_y = st.slider("Box Y (Å)", 10.0, 50.0, 20.0, step=1.0, key="pipe_dock_box_y")
                with bx3:
                    d_box_z = st.slider("Box Z (Å)", 10.0, 50.0, 20.0, step=1.0, key="pipe_dock_box_z")

                if st.button("🚀 Start Docking with Selected Clusters", type="primary",
                             use_container_width=True, key="pipe_start_docking"):
                    if not ligand_files:
                        st.error("❌ Please upload at least one ligand file.")
                    else:
                        dock_job_id = f"dock_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
                        ligand_dir = os.path.join(UPLOAD_DIR, dock_job_id, 'ligands')
                        os.makedirs(ligand_dir, exist_ok=True)
                        collected = []
                        for lf in ligand_files:
                            lf_path = os.path.join(ligand_dir, lf.name)
                            with open(lf_path, 'wb') as f:
                                f.write(lf.getbuffer())
                            if lf.name.endswith('.zip'):
                                with zipfile.ZipFile(lf_path, 'r') as zfh:
                                    zfh.extractall(ligand_dir)
                                    for fname in zfh.namelist():
                                        if fname.endswith('.pdbqt'):
                                            collected.append(os.path.join(ligand_dir, fname))
                            elif lf.name.endswith('.pdbqt'):
                                collected.append(lf_path)

                        if len(collected) > Config.MAX_DOCKING_LIGANDS:
                            st.warning(f"⚠️ Capped to {Config.MAX_DOCKING_LIGANDS} ligands.")
                            for excess in collected[Config.MAX_DOCKING_LIGANDS:]:
                                try:
                                    os.remove(excess)
                                except OSError:
                                    pass
                            collected = collected[:Config.MAX_DOCKING_LIGANDS]

                        # Build filtered reps CSV for selected clusters only
                        if 'cluster' in df_reps.columns:
                            filtered_reps = df_reps[
                                df_reps['cluster'].isin([int(c) for c in selected_clusters])
                            ]
                        else:
                            filtered_reps = df_reps
                        dock_upload_dir = os.path.join(UPLOAD_DIR, dock_job_id)
                        os.makedirs(dock_upload_dir, exist_ok=True)
                        filtered_reps_file = os.path.join(dock_upload_dir, 'filtered_reps.csv')
                        filtered_reps.to_csv(filtered_reps_file, index=False)

                        pdb_source_dir = os.path.join(RESULTS_DIR, results_job_id, 'pdbs')

                        dock_task_obj = run_docking_task.delay(
                            cluster_representatives_csv=filtered_reps_file,
                            ligand_folder=ligand_dir,
                            job_id=dock_job_id,
                            smina_exe_path=Config.SMINA_PATH,
                            num_poses=d_poses,
                            exhaustiveness=d_exhaust,
                            ph_value=d_ph,
                            box_size_x=d_box_x,
                            box_size_y=d_box_y,
                            box_size_z=d_box_z,
                            pdb_source_dir=pdb_source_dir,
                        )
                        st.session_state.pipe_docking_task_id = dock_task_obj.id
                        st.session_state.pipe_docking_job_id = dock_job_id
                        st.rerun()
            else:
                st.info("← Select clusters in the heatmap above to configure and launch docking here.")

    except Exception as e:
        st.error(f"Error loading clustering results: {e}")
        logger.error(f"Pipeline inline cluster results error: {e}", exc_info=True)


# Session state
initialize_session_state()

if 'pipeline_job_id' not in st.session_state:
    st.session_state.pipeline_job_id = None
if 'pipeline_task_id' not in st.session_state:
    st.session_state.pipeline_task_id = None
if 'pipeline_status' not in st.session_state:
    st.session_state.pipeline_status = 'idle'


# Real progress boundaries matching _run_stage calls in tasks.py:
#   0-25   extract_to_pdb
#   25-60  detect_pockets
#   60-80  cluster_pockets
#   80-97  docking (optional)
_STAGES = [
    ('Extract Frames',    0,  25, 'file-earmark-arrow-down'),
    ('Detect Pockets',   25,  60, 'search'),
    ('Cluster Pockets',  60,  80, 'diagram-3'),
    ('Dock (optional)',  80, 100, 'flask'),
]


def _stage_class(progress, start, end):
    if progress >= end:
        return 'stage-done'
    elif progress >= start:
        return 'stage-active'
    return 'stage-pending'


def show_stage_indicators(progress, current_step=''):
    boxes = "".join(
        f'<div class="stage-box {_stage_class(progress, s, e)}">{name}</div>'
        for name, s, e, _ in _STAGES
    )
    st.markdown(f'<div class="stage-indicator">{boxes}</div>', unsafe_allow_html=True)
    if current_step:
        st.caption(f"▶ {current_step}")


# ── Status banner if a task is running ─────────────────────────────────
if st.session_state.pipeline_task_id:
    try:
        _task = celery_app.AsyncResult(st.session_state.pipeline_task_id)
        if _task.state == 'PENDING':
            show_stage_indicators(0, 'Waiting in queue…')
            st.progress(0, text="Pending…")
            st.info("⏳ Pipeline task is pending in the Celery queue.")
            time.sleep(4)
            st.rerun()
        elif _task.state == 'PROGRESS':
            _info = _task.info or {}
            _prog = _info.get('progress', 0)
            _step = _info.get('current_step', 'Processing…')
            _stage = _info.get('stage', '')

            show_stage_indicators(_prog, _step)
            st.progress(_prog / 100, text=f"{_prog}%")

            # Contextual metrics depending on stage
            if _stage in ('detect', 'cluster', 'cluster_done', 'docking'):
                _frames = _info.get('frames_extracted')
                if _frames is not None:
                    st.caption(f"📁 {_frames} frames extracted")
            if _stage in ('cluster', 'cluster_done', 'docking'):
                _pockets = _info.get('pockets_detected')
                if _pockets is not None:
                    st.caption(f"🔍 {_pockets} pockets detected")
            if _stage == 'docking':
                _pairs_done = _info.get('pairs_done', 0)
                _pairs_total = _info.get('pairs_total', 0)
                if _pairs_total > 0:
                    st.progress(_pairs_done / _pairs_total,
                                text=f"Docking: {_pairs_done}/{_pairs_total} pairs")

            time.sleep(3)
            st.rerun()
        elif _task.state == 'SUCCESS':
            result = _task.result or {}
            show_stage_indicators(100, 'All stages complete')
            st.success("✅ Full pipeline completed successfully!")
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Frames extracted", result.get('frames_extracted', '—'))
            col2.metric("Pockets detected", result.get('pockets_detected', '—'))
            col3.metric("Cluster representatives", result.get('representatives', '—'))
            col4.metric("Docking poses", result.get('docking_poses', '—') or '—')
            if result.get('docking_error'):
                st.warning(f"⚠️ Docking encountered an error: {result['docking_error']}")
            # Partial-success callout: surface per-pair failures (if any)
            from failure_view import render_pair_failures_callout
            render_pair_failures_callout(result, st.session_state.pipeline_job_id)

            cluster_job = result.get('cluster_job_id', st.session_state.pipeline_job_id)
            if cluster_job:
                _show_pipeline_cluster_inline(cluster_job)
        elif _task.state == 'FAILURE':
            show_stage_indicators(0, 'Pipeline failed')
            from failure_view import load_status_json, render_task_failure
            _info = _task.info if isinstance(_task.info, dict) else {}
            render_task_failure(
                _info,
                load_status_json(st.session_state.pipeline_job_id),
                st.session_state.pipeline_job_id,
            )
    except Exception as e:
        st.warning(f"Could not retrieve task status: {e}")

    if st.button("🔄 Reset / Start New Pipeline"):
        st.session_state.pipeline_task_id = None
        st.session_state.pipeline_job_id = None
        st.session_state.pipeline_status = 'idle'
        st.session_state.pipe_docking_task_id = None
        st.session_state.pipe_docking_job_id = None
        st.session_state.docking_target_clusters = []
        st.session_state.cluster_preview_id = None
        st.rerun()

    st.stop()


# ── Input form ──────────────────────────────────────────────────────────
st.markdown("### 📁 Input Files")

with st.form("pipeline_form"):
    col_traj, col_topo = st.columns(2)

    with col_traj:
        traj_file = st.file_uploader(
            "Trajectory (.xtc)",
            type=['xtc'],
            help="GROMACS XTC trajectory file"
        )
    with col_topo:
        topo_file = st.file_uploader(
            "Topology (.pdb or .gro)",
            type=['pdb', 'gro'],
            help="Topology/structure file matching the trajectory"
        )

    st.markdown("### ⚙️ Processing Parameters")

    col_stride, col_threads = st.columns(2)
    with col_stride:
        stride = st.slider("Frame Stride", min_value=1, max_value=100, value=10,
                           help="Extract every Nth frame from trajectory")
    with col_threads:
        num_threads = st.slider("CPU Threads", min_value=1, max_value=16, value=4,
                                help="Number of parallel threads for pocket detection")

    col_prob, col_method = st.columns(2)
    with col_prob:
        min_prob = st.slider("Min Pocket Probability", min_value=0.0, max_value=1.0, value=0.5, step=0.05,
                             help="Minimum p2rank probability threshold")
    with col_method:
        clustering_method = st.selectbox("Clustering Method", options=['dbscan', 'kmeans', 'hierarchical'],
                                         help="Algorithm for grouping similar pockets")

    st.markdown("### 🔬 Optional: Molecular Docking")
    include_docking = st.checkbox("Run docking after clustering", value=False)
    if include_docking:
        st.info("ℹ️ Docking will run automatically on **all** cluster representatives. To hand-pick specific clusters first, leave this unchecked — then use the heatmap in Step 2 to select clusters before docking.")

    ligand_files_uploaded = None
    num_poses = 10
    exhaustiveness = 8
    ph_value = 7.4
    box_size = 20.0

    if include_docking:
        st.info(f"Ligand limit: {Config.MAX_DOCKING_LIGANDS} files. Exhaustiveness cap: {Config.MAX_DOCKING_EXHAUSTIVENESS}.")
        ligand_files_uploaded = st.file_uploader(
            "Ligand Files (PDBQT or ZIP)",
            type=['pdbqt', 'zip'],
            accept_multiple_files=True,
            help="Upload PDBQT ligands or a ZIP archive of PDBQT files"
        )
        dock_col1, dock_col2, dock_col3, dock_col4 = st.columns(4)
        with dock_col1:
            num_poses = st.slider("Poses per Ligand", 1, 20, 10)
        with dock_col2:
            exhaustiveness = st.slider("Exhaustiveness", 1, Config.MAX_DOCKING_EXHAUSTIVENESS,
                                       min(8, Config.MAX_DOCKING_EXHAUSTIVENESS))
        with dock_col3:
            ph_value = st.slider("pH", 4.0, 10.0, 7.4, step=0.1)
        with dock_col4:
            box_size = st.slider("Box Size (Å)", 10.0, 50.0, 20.0, step=1.0)

    submitted = st.form_submit_button("🚀 Launch Full Pipeline", type="primary", use_container_width=True)


if submitted:
    if not traj_file:
        st.error("❌ Please upload a trajectory (.xtc) file.")
        st.stop()
    if not topo_file:
        st.error("❌ Please upload a topology (.pdb or .gro) file.")
        st.stop()
    if include_docking and not ligand_files_uploaded:
        st.error("❌ Please upload at least one ligand file to enable docking.")
        st.stop()

    # Rate limit check
    try:
        check_task_rate_limit()
    except RateLimitExceeded as e:
        st.error(f"⏳ Task rate limit exceeded: {e}")
        st.stop()

    job_id = f"pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}"
    job_upload_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_upload_dir, exist_ok=True)

    # Save trajectory
    traj_path = os.path.join(job_upload_dir, traj_file.name)
    with open(traj_path, 'wb') as f:
        f.write(traj_file.getbuffer())

    # Save topology
    topo_path = os.path.join(job_upload_dir, topo_file.name)
    with open(topo_path, 'wb') as f:
        f.write(topo_file.getbuffer())

    # Save ligands if provided
    ligand_folder = None
    if include_docking and ligand_files_uploaded:
        ligand_dir = os.path.join(job_upload_dir, 'ligands')
        os.makedirs(ligand_dir, exist_ok=True)
        collected = []
        for lf in ligand_files_uploaded:
            lf_path = os.path.join(ligand_dir, lf.name)
            with open(lf_path, 'wb') as f:
                f.write(lf.getbuffer())
            if lf.name.endswith('.zip'):
                with zipfile.ZipFile(lf_path, 'r') as zf:
                    zf.extractall(ligand_dir)
                    for fname in zf.namelist():
                        if fname.endswith('.pdbqt'):
                            collected.append(os.path.join(ligand_dir, fname))
            elif lf.name.endswith('.pdbqt'):
                collected.append(lf_path)
        if len(collected) > Config.MAX_DOCKING_LIGANDS:
            st.warning(f"⚠️ Ligand count capped from {len(collected)} to {Config.MAX_DOCKING_LIGANDS}.")
            for excess in collected[Config.MAX_DOCKING_LIGANDS:]:
                try:
                    os.remove(excess)
                except OSError:
                    pass
            collected = collected[:Config.MAX_DOCKING_LIGANDS]
        ligand_folder = ligand_dir

    # Submit Celery task
    task = run_pockethunter_pipeline.delay(
        xtc_file_path=traj_path,
        topology_file_path=topo_path,
        job_id=job_id,
        stride=stride,
        num_threads=num_threads,
        min_prob=min_prob,
        clustering_method=clustering_method,
        run_docking=include_docking,
        ligand_folder=ligand_folder,
        num_poses=num_poses,
        exhaustiveness=exhaustiveness,
        ph_value=ph_value,
        box_size_x=box_size,
        box_size_y=box_size,
        box_size_z=box_size,
    )

    st.session_state.pipeline_job_id = job_id
    st.session_state.pipeline_task_id = task.id
    st.session_state.pipeline_status = 'running'
    st.session_state.cached_job_ids['pipeline'] = job_id

    st.success(f"✅ Pipeline started! Job ID: `{job_id}`")
    time.sleep(1)
    st.rerun()
