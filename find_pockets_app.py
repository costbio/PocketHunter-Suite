"""Step 1: Find Pockets — merged Extract + Detect page.

Replaces the legacy split between extract_frames_app.py and detect_pockets_app.py.
Two input modes selected via radio:
  - From trajectory (XTC + topology) → extract frames → detect pockets
  - From PDB ZIP archive → detect pockets only

Both modes produce the same on-disk output (``results/<job>/pockets/pockets.csv``)
so Step 2 (Cluster Pockets) consumes them identically.
"""
import os
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from celery_app import celery_app
from config import Config
from logging_config import setup_logging
from rate_limiter import RateLimitExceeded, check_task_rate_limit
from security import FileValidator, SecurityError, handle_file_upload_secure
from session_state import initialize_session_state, render_load_previous_widget
from tasks import run_find_pockets_task

initialize_session_state()
logger = setup_logging(__name__)

UPLOAD_DIR = str(Config.UPLOAD_DIR)
RESULTS_DIR = str(Config.RESULTS_DIR)

# ── Header (brutalist, matches main.py) ───────────────────────────────────
st.markdown(
    """
<div class="bh" style="margin-top: 4px;">
    <div class="bh-row">
        <span class="bh-title">Step 1 / Find Pockets</span>
        <span class="bh-version">[MD → POCKETS]</span>
    </div>
    <div class="bh-rule"></div>
    <div class="bh-stages">
        Extract frames from a trajectory <span class="sep">•</span>
        or start from PDB structures <span class="sep">•</span>
        run P2Rank pocket detection
    </div>
</div>
""",
    unsafe_allow_html=True,
)

# Surface the current job ID, if any
if st.session_state.find_pockets_job_id:
    st.caption(f"Current job: `{st.session_state.find_pockets_job_id}`")

# ── Mode selector ────────────────────────────────────────────────────────
mode_label = st.radio(
    "Input source",
    ["From trajectory (XTC + topology)", "From PDB ZIP archive"],
    horizontal=True,
    key="fp_mode",
    help=(
        "Trajectory mode runs frame extraction then pocket detection. "
        "PDB ZIP mode skips extraction — use it when you already have a folder "
        "of PDB structures (e.g. from another MD pipeline)."
    ),
)
mode_is_trajectory = mode_label.startswith("From trajectory")

# ── Inputs ───────────────────────────────────────────────────────────────
st.markdown("### Inputs")

xtc_file = None
topology_file = None
stride = 10
pdb_zip = None

if mode_is_trajectory:
    col1, col2 = st.columns(2)
    with col1:
        xtc_file = st.file_uploader(
            "Trajectory (.xtc)",
            type=["xtc"],
            key="fp_xtc",
            help="Molecular dynamics trajectory file",
        )
    with col2:
        topology_file = st.file_uploader(
            "Topology (.pdb, .gro)",
            type=["pdb", "gro"],
            key="fp_top",
            help="Reference topology / starting structure",
        )
    stride = st.number_input(
        "Frame stride",
        min_value=1,
        value=10,
        key="fp_stride",
        help="Extract every Nth frame from the trajectory.",
    )
else:
    pdb_zip = st.file_uploader(
        "PDB structures (.zip)",
        type=["zip"],
        key="fp_zip",
        help="ZIP archive of .pdb files — extraction will be skipped.",
    )

num_threads = st.number_input(
    "Threads",
    min_value=1,
    max_value=16,
    value=4,
    key="fp_threads",
    help="CPU threads to allocate for pocket detection.",
)

# ── Submit ───────────────────────────────────────────────────────────────
st.markdown("---")
if st.button("Find Pockets", type="primary", use_container_width=True):
    job_id = f"find_pockets_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    xtc_path = topology_path = pdb_input_dir = None

    if mode_is_trajectory:
        if not xtc_file or not topology_file:
            st.error("Both trajectory and topology files are required.")
            st.stop()
        try:
            xtc_path = str(handle_file_upload_secure(xtc_file, job_id, "trajectory_"))
            topology_path = str(handle_file_upload_secure(topology_file, job_id, "topology_"))
        except RateLimitExceeded as e:
            st.error(f"Rate limit exceeded — wait {e.retry_after:.0f}s.")
            st.stop()
        except SecurityError as e:
            st.error(f"File upload failed: {e}")
            st.stop()
    else:
        if not pdb_zip:
            st.error("Upload a ZIP archive containing PDB files.")
            st.stop()
        try:
            zip_path = handle_file_upload_secure(pdb_zip, job_id, "pdbs_")
            FileValidator.validate_zip_file(Path(str(zip_path)))
        except SecurityError as e:
            st.error(f"ZIP upload failed: {e}")
            st.stop()

        pdb_input_dir = os.path.join(UPLOAD_DIR, job_id, "extracted_pdbs")
        os.makedirs(pdb_input_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Pre-scan: fail fast if there are no .pdb entries before we
            # write anything to disk.
            if not any(name.lower().endswith(".pdb") for name in zf.namelist()):
                st.error(
                    "No PDB files in this ZIP. Make sure your archive contains "
                    "`.pdb` structures at any depth."
                )
                st.stop()
            zf.extractall(pdb_input_dir)

        # Second line of defense — catches the rare case where namelist() lied
        # about file extensions (e.g. AppleDouble metadata in macOS archives).
        pdb_count = sum(1 for _ in Path(pdb_input_dir).rglob("*.pdb"))
        if pdb_count == 0:
            st.error("No PDB files found after extracting the ZIP.")
            st.stop()

    try:
        check_task_rate_limit()
    except RateLimitExceeded as e:
        st.error(f"Task rate limit exceeded — wait {e.retry_after:.0f}s.")
        st.stop()

    task = run_find_pockets_task.delay(
        job_id=job_id,
        xtc_file_path=xtc_path,
        topology_file_path=topology_path,
        pdb_input_dir=pdb_input_dir,
        stride=int(stride),
        num_threads=int(num_threads),
    )
    st.session_state.find_pockets_job_id = job_id
    st.session_state.find_pockets_task_id = task.id
    st.session_state.find_pockets_status = "running"
    st.success(f"Started job `{job_id}`.")
    st.rerun()

# ── Status / progress ────────────────────────────────────────────────────
if st.session_state.find_pockets_task_id:
    try:
        task = celery_app.AsyncResult(st.session_state.find_pockets_task_id)
    except Exception as e:
        st.error(f"Could not read task state: {e}")
        if st.button("Clear task and start over"):
            st.session_state.find_pockets_task_id = None
            st.session_state.find_pockets_status = "idle"
            st.rerun()
        st.stop()

    info = task.info if isinstance(task.info, dict) else {}
    state = task.state

    if state == "PENDING":
        st.info("Queued — waiting for a worker.")
        st.progress(0.0)
    elif state == "PROGRESS":
        prog = info.get("progress", 0) / 100
        step = info.get("current_step", "Working…")
        stage = info.get("stage", "")
        st.info(f"{step}  *(stage: {stage})*" if stage else step)
        st.progress(min(max(prog, 0.0), 1.0))
    elif state == "SUCCESS":
        result = task.result or {}
        st.success(
            f"Completed in {result.get('processing_time', 0):.1f}s — "
            f"detected {result.get('pockets_detected', 0)} pockets across "
            f"{result.get('frames_extracted', 0)} structures."
        )
        st.progress(1.0)
        st.session_state.find_pockets_status = "completed"
        st.session_state.cached_job_ids["find_pockets"] = st.session_state.find_pockets_job_id
    elif state == "FAILURE":
        from failure_view import load_status_json, render_task_failure
        render_task_failure(
            info,
            load_status_json(st.session_state.find_pockets_job_id),
            st.session_state.find_pockets_job_id,
        )
        st.session_state.find_pockets_status = "failed"

    # Cancel option for live tasks
    if state in ("PENDING", "PROGRESS"):
        if st.button("Cancel task"):
            try:
                task.revoke(terminate=True)
                st.session_state.find_pockets_status = "cancelled"
                st.session_state.find_pockets_task_id = None
                st.rerun()
            except Exception as e:
                st.error(f"Could not cancel: {e}")

# ── Results ──────────────────────────────────────────────────────────────
render_load_previous_widget(
    session_key="find_pockets_job_id",
    label="Load previous results",
    placeholder="e.g. find_pockets_20260511_120000_abcd1234",
    text_input_key="fp_load_id",
    button_key="fp_load_btn",
)

results_job_id = st.session_state.find_pockets_job_id

if results_job_id:
    # Defense in depth: even if the ID came from session state (set elsewhere),
    # validate before joining into a path.
    from security import FileValidator, SecurityError
    try:
        results_job_id = FileValidator.validate_job_id(results_job_id)
    except SecurityError as e:
        st.error(f"Invalid job ID in session: {e}")
        st.stop()
    pockets_csv = os.path.join(RESULTS_DIR, results_job_id, "pockets", "pockets.csv")
    if os.path.exists(pockets_csv):
        try:
            df_pockets = pd.read_csv(pockets_csv)
        except Exception as e:
            st.error(f"Could not read pockets.csv: {e}")
            df_pockets = pd.DataFrame()

        if "residues" in df_pockets.columns and df_pockets["residues"].dtype == object:
            df_pockets["num_residues"] = df_pockets["residues"].apply(
                lambda x: len(str(x).split()) if pd.notna(x) else 0
            )
        elif "residues" in df_pockets.columns:
            df_pockets["num_residues"] = df_pockets["residues"]
        else:
            df_pockets["num_residues"] = 0

        if len(df_pockets) == 0:
            st.warning("Detection completed but no pockets were found in the input structures.")
            st.info(
                "**What to try next:**\n"
                "- Lower the `stride` parameter to extract more frames (more chances to find pockets).\n"
                "- Verify the topology and trajectory match (same atom count, residue numbering).\n"
                "- If a previous run failed silently, open Task Monitor and inspect the error log."
            )
        else:
            st.markdown("---")
            st.markdown("### Detection Results")

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total Pockets", len(df_pockets))
            c2.metric("Avg Probability", f"{df_pockets['probability'].mean():.3f}")
            c3.metric("High Confidence (≥0.7)", int((df_pockets["probability"] >= 0.7).sum()))
            c4.metric("Best Probability", f"{df_pockets['probability'].max():.3f}")

            tab_table, tab_dist, tab_dl = st.tabs(["Pocket Table", "Distribution", "Download"])

            with tab_table:
                def _badge(p):
                    if p >= 0.7:
                        return "High"
                    if p >= 0.4:
                        return "Medium"
                    return "Low"

                df_disp = df_pockets.sort_values("probability", ascending=False).copy()
                df_disp["Confidence"] = df_disp["probability"].apply(_badge)

                col_a, col_b = st.columns(2)
                with col_a:
                    min_p = st.slider("Minimum probability", 0.0, 1.0, 0.0, 0.05, key="fp_minp")
                with col_b:
                    conf_pick = st.multiselect(
                        "Filter by confidence",
                        options=["High", "Medium", "Low"],
                        default=["High", "Medium", "Low"],
                        key="fp_conf",
                    )

                df_f = df_disp[df_disp["probability"] >= min_p]
                if conf_pick:
                    df_f = df_f[df_f["Confidence"].isin(conf_pick)]

                cols_to_show = [c for c in ["File name", "pocket_index", "probability",
                                            "num_residues", "Confidence"] if c in df_f.columns]
                st.dataframe(df_f[cols_to_show], use_container_width=True, height=400)
                st.caption(f"Showing {len(df_f)} of {len(df_pockets)} pockets.")

            with tab_dist:
                col_l, col_r = st.columns(2)
                with col_l:
                    fig_h = px.histogram(df_pockets, x="probability", nbins=30,
                                         title="Probability distribution",
                                         color_discrete_sequence=["#000000"])
                    fig_h.update_layout(showlegend=False)
                    st.plotly_chart(fig_h, use_container_width=True)
                with col_r:
                    fig_b = px.box(df_pockets, y="num_residues",
                                   title="Residue count distribution",
                                   color_discrete_sequence=["#000000"])
                    fig_b.update_layout(showlegend=False)
                    st.plotly_chart(fig_b, use_container_width=True)

                fig_s = px.scatter(
                    df_pockets, x="num_residues", y="probability",
                    size="probability", color="probability",
                    title="Pocket probability vs size",
                    labels={"num_residues": "Residues", "probability": "Probability"},
                    color_continuous_scale="Greys",
                    hover_data=[c for c in ["File name", "pocket_index"] if c in df_pockets.columns],
                )
                st.plotly_chart(fig_s, use_container_width=True)

            with tab_dl:
                col_a, col_b = st.columns(2)
                with col_a:
                    csv_data = df_pockets.to_csv(index=False)
                    st.download_button(
                        "Download pockets.csv",
                        data=csv_data,
                        file_name=f"pockets_{results_job_id}.csv",
                        mime="text/csv",
                        use_container_width=True,
                    )
                    df_hc = df_pockets[df_pockets["probability"] >= 0.7]
                    if len(df_hc) > 0:
                        st.download_button(
                            "Download high-confidence subset (CSV)",
                            data=df_hc.to_csv(index=False),
                            file_name=f"high_confidence_pockets_{results_job_id}.csv",
                            mime="text/csv",
                            use_container_width=True,
                        )
                with col_b:
                    pockets_dir = os.path.join(RESULTS_DIR, results_job_id, "pockets")
                    pdbs_dir = os.path.join(RESULTS_DIR, results_job_id, "pdbs")
                    zip_path = os.path.join(pockets_dir, "pockets_pdbs.zip")
                    if st.button("Generate PDB archive", use_container_width=True, key="fp_genzip"):
                        if os.path.isdir(pdbs_dir):
                            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                                for p in Path(pdbs_dir).glob("*.pdb"):
                                    zf.write(p, p.name)
                            st.success("Archive created.")
                        else:
                            st.warning("No /pdbs directory for this job (PDB ZIP mode keeps inputs in /uploads).")
                    if os.path.exists(zip_path):
                        with open(zip_path, "rb") as f:
                            st.download_button(
                                "Download all PDB files (ZIP)",
                                data=f.read(),
                                file_name=f"pockets_pdbs_{results_job_id}.zip",
                                mime="application/zip",
                                use_container_width=True,
                            )

            st.markdown("---")
            cont_col1, cont_col2 = st.columns([3, 1])
            with cont_col1:
                st.info(
                    f"Job `{results_job_id}` is ready to feed Step 2 (Cluster Pockets). "
                    "Its ID will pre-fill there."
                )
            with cont_col2:
                if st.button("Continue to Step 2 →", type="primary",
                             use_container_width=True, key="fp_continue"):
                    st.session_state.pending_nav = "Step 2: Cluster Pockets"
                    st.rerun()

# ── Auto-refresh while running ───────────────────────────────────────────
if (
    st.session_state.find_pockets_status == "running"
    and st.session_state.find_pockets_task_id
):
    try:
        t = celery_app.AsyncResult(st.session_state.find_pockets_task_id)
        if t.ready():
            st.session_state.find_pockets_status = "completed" if t.successful() else "failed"
            st.rerun()
        else:
            time.sleep(3)
            st.rerun()
    except Exception:
        time.sleep(3)
        st.rerun()
