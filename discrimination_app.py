"""
discrimination_app.py — Step 4: Discrimination Analysis

Users provide their cluster job ID, upload actives and decoys SDF files,
and the app ranks cluster representatives by pharmacophore-based active/decoy
discrimination (ROC-AUC, EF1%, EF5%).
"""

import streamlit as st
import os
import uuid
import json
import zipfile
import time
import pandas as pd
import plotly.graph_objects as go
from pathlib import Path

from tasks import run_discrimination_task
from celery_app import celery_app
from config import Config
from session_state import initialize_session_state
from logging_config import setup_logging

RESULTS_DIR = str(Config.RESULTS_DIR)
UPLOAD_DIR = str(Config.UPLOAD_DIR)
logger = setup_logging(__name__)

initialize_session_state()

# ── Page header ──────────────────────────────────────────────────────────────

st.markdown("""
<div style="background: linear-gradient(135deg, #1565C0 0%, #2E7D32 100%);
            padding: 2rem; border-radius: 16px; margin-bottom: 2rem;
            color: white; text-align: center;">
    <h1 style="margin:0; font-size:2rem;"> Discrimination Analysis</h1>
    <p style="margin:0.5rem 0 0 0; opacity:0.9;">
        Rank conformations by their ability to distinguish active from decoy ligands
    </p>
</div>
""", unsafe_allow_html=True)

# ── Helper ───────────────────────────────────────────────────────────────────

def _show_results(job_id, result_info):
    """Render ranked conformations table, ROC-AUC bar chart, and download."""
    results_csv = result_info.get('discrimination_results_csv')
    if not results_csv or not os.path.exists(results_csv):
        st.warning("Results file not found.")
        return

    df = pd.read_csv(results_csv)
    st.success(f" Discrimination complete — {len(df)} conformations ranked.")

    st.markdown("### Ranked Conformations")

    def _colour_auc(val):
        if val >= 0.7:
            return 'background-color: #c8e6c9'
        if val >= 0.6:
            return 'background-color: #fff9c4'
        return ''

    display_cols = [c for c in ['cluster_id', 'frame', 'roc_auc', 'ef1', 'ef5'] if c in df.columns]
    st.dataframe(
        df[display_cols].style.map(_colour_auc, subset=['roc_auc']),
        use_container_width=True,
    )

    st.markdown("### ROC-AUC per Conformation")
    fig = go.Figure(go.Bar(
        x=[f"Cluster {row['cluster_id']} (Fr.{row['frame']})" for _, row in df.iterrows()],
        y=df['roc_auc'],
        marker_color=[
            '#2E7D32' if v >= 0.7 else '#F57C00' if v >= 0.6 else '#C62828'
            for v in df['roc_auc']
        ],
    ))
    fig.update_layout(
        xaxis_title="Conformation",
        yaxis_title="ROC-AUC",
        yaxis=dict(range=[0, 1]),
        height=350,
    )
    fig.add_hline(y=0.5, line_dash='dash', line_color='grey', annotation_text='Random')
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("### Download Top Conformations")
    n_top = st.slider(
        "Number of top conformations to include in ZIP",
        1, min(5, len(df)), min(3, len(df)),
        key="disc_n_top"
    )

    top_dir = os.path.join(RESULTS_DIR, job_id, 'discrimination')
    if st.button(" Prepare ZIP", key="disc_prepare_zip"):
        top_df = df.head(n_top)
        zip_path = os.path.join(top_dir, 'top_conformations.zip')
        extract_job_id = st.session_state.cached_job_ids.get('extract', '')
        pdb_dir = os.path.join(RESULTS_DIR, extract_job_id, 'pdbs') if extract_job_id else ''

        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.write(results_csv, 'discrimination_results.csv')
            for _, row in top_df.iterrows():
                frame = str(row['frame'])
                if pdb_dir and os.path.isdir(pdb_dir):
                    matches = [f for f in os.listdir(pdb_dir) if f'_{frame}.pdb' in f]
                    for pdb_file in matches:
                        zf.write(
                            os.path.join(pdb_dir, pdb_file),
                            f"top_conformations/cluster_{row['cluster_id']}_{pdb_file}"
                        )

        with open(zip_path, 'rb') as f:
            st.download_button(
                "⬇ Download ZIP",
                data=f.read(),
                file_name=f"top_conformations_{job_id}.zip",
                mime="application/zip",
                key="disc_download_zip"
            )

# ── Input section ────────────────────────────────────────────────────────────

st.subheader("1. Provide Clustering Results")

cluster_job_id_input = st.text_input(
    "Cluster Job ID",
    value=st.session_state.cached_job_ids.get('cluster') or '',
    placeholder="e.g. 20260407_123456_abcd",
    help="The Job ID from Step 3: Cluster Pockets. Auto-filled if you ran clustering this session.",
)

cluster_valid = False
if cluster_job_id_input:
    reps_csv = os.path.join(
        RESULTS_DIR, cluster_job_id_input, 'pocket_clusters', 'cluster_representatives.csv'
    )
    if os.path.exists(reps_csv):
        df_reps_preview = pd.read_csv(reps_csv)
        st.success(f" Found {len(df_reps_preview)} cluster representatives.")
        cluster_valid = True
    else:
        st.error(" No cluster_representatives.csv found for this Job ID. Run Step 3 first.")

st.subheader("2. Upload Ligand Sets")

col1, col2 = st.columns(2)
with col1:
    actives_file = st.file_uploader(
        "Actives (SDF)", type=["sdf"],
        help="Known active ligands. Max 200 molecules.",
        key="disc_actives_upload"
    )
with col2:
    decoys_file = st.file_uploader(
        "Decoys (SDF)", type=["sdf"],
        help="Decoy (non-binding) ligands. Max 2000 molecules.",
        key="disc_decoys_upload"
    )

# ── Decoy quality guidance ───────────────────────────────────────────────────

with st.expander("️ Decoy quality matters — read before running", expanded=False):
    st.markdown("""
**Pharmacophore complementarity is a broad-class discriminator.**
Its ability to separate actives from decoys depends almost entirely on
how different the decoys are from the actives in feature space.

| Decoy type | Expected ROC-AUC | Example |
|---|---|---|
| Drug-like (other targets) | ~0.5–0.6 | ChEMBL GPCR/protease ligands |
| Property-matched (DUD-E) | ~0.55–0.70 | dude.docking.org sets |
| Diverse / non-drug-like | ~0.7–0.9 | Fragments, natural products, metabolites |

**Why drug-like decoys fail:** All drug-like molecules share similar pharmacophore
feature distributions (predominantly hydrophobic, 2–4 H-bond donors/acceptors).
The 1D residue-counting representation cannot distinguish VEGFR2 inhibitors
from COX-2 inhibitors — both are mostly hydrophobic with a few polar groups.

**Recommendations:**
- Use [DUD-E](https://dude.docking.org/) decoys for your target if available —
  they are property-matched but scaffold-diverse.
- Include structurally simple molecules (fragments, metabolites) alongside
  drug-like decoys to broaden the separation.
- Focus on the **relative ranking** of conformations (which cluster scores
  highest?) rather than the absolute ROC-AUC value.
- A ROC-AUC > 0.6 with drug-like decoys is a meaningful result.
""")

# ── Launch ───────────────────────────────────────────────────────────────────

st.subheader("3. Run Discrimination")

can_launch = cluster_valid and actives_file is not None and decoys_file is not None
disc_task_id = st.session_state.get('discrimination_task_id')
disc_job_id = st.session_state.get('discrimination_job_id')

if st.button("▶ Run Discrimination Analysis", disabled=not can_launch,
             type="primary", key="disc_launch"):
    job_id = f"disc_{uuid.uuid4().hex[:8]}"
    upload_job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(upload_job_dir, exist_ok=True)

    actives_path = os.path.join(upload_job_dir, 'actives.sdf')
    decoys_path = os.path.join(upload_job_dir, 'decoys.sdf')

    with open(actives_path, 'wb') as f:
        f.write(actives_file.getbuffer())
    with open(decoys_path, 'wb') as f:
        f.write(decoys_file.getbuffer())

    task = run_discrimination_task.delay(
        cluster_job_id=cluster_job_id_input,
        actives_path=actives_path,
        decoys_path=decoys_path,
        job_id=job_id,
        extract_job_id=st.session_state.cached_job_ids.get('extract') or None,
    )

    st.session_state.discrimination_task_id = task.id
    st.session_state.discrimination_job_id = job_id
    st.session_state.discrimination_status = 'running'
    st.session_state.cached_job_ids['discrimination'] = job_id
    st.rerun()

# ── Status polling ───────────────────────────────────────────────────────────

if disc_task_id:
    task_result = celery_app.AsyncResult(disc_task_id)
    state = task_result.state
    meta = task_result.info or {}

    if state == 'PROGRESS':
        progress = meta.get('progress', 0)
        step = meta.get('current_step', 'Running…')
        st.progress(progress / 100, text=f"⏳ {step}")
        time.sleep(2)
        st.rerun()

    elif state == 'SUCCESS':
        st.session_state.discrimination_status = 'completed'
        _show_results(disc_job_id, task_result.result or {})

    elif state in ('FAILURE', 'REVOKED'):
        err = meta.get('exc_message', str(meta)) if isinstance(meta, dict) else str(meta)
        st.error(f" Discrimination failed: {err}")
        if st.button(" Reset", key="disc_reset"):
            st.session_state.discrimination_task_id = None
            st.session_state.discrimination_job_id = None
            st.session_state.discrimination_status = 'idle'
            st.rerun()

    elif state == 'PENDING':
        st.info("⏳ Job queued — waiting for a worker…")
        time.sleep(3)
        st.rerun()

    else:
        # Completed in a prior session — reload from status file
        if st.session_state.discrimination_status == 'completed' and disc_job_id:
            status_file = os.path.join(RESULTS_DIR, disc_job_id + '_status.json')
            if os.path.exists(status_file):
                with open(status_file) as f:
                    status_data = json.load(f)
                _show_results(disc_job_id, status_data.get('result_info', {}))
