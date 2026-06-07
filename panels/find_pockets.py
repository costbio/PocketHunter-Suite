"""Find-Pockets stage panel (v2 analysis app).

Three states selected by ``st.session_state.find_pockets_task_id`` and
the presence of a completed find_pockets Job row:

    settings → running → results

A "Re-run with new settings" affordance on the results state clears the
in-memory task/job keys and returns the user to the settings form.

Trajectory + topology + stride OR PDB-ZIP-only input modes are both
supported (the legacy page covers both; dropping one would be regression
for users who arrive with pre-extracted PDBs).
"""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from config import Config
from rate_limiter import RateLimitExceeded, check_task_rate_limit
from security import FileValidator, SecurityError, handle_file_upload_secure
from session_routes import register_session_job
from tasks import run_find_pockets_task

from panels._shared import (
    PoolCapHit,
    _normalise_pdb_filename,
    assert_submit_allowed,
    get_async_result,
    job_by_legacy_id,
    latest_job_of_kind,
    new_job_id,
    render_failure,
    render_pool_cap_error,
    render_running_progress,
    viewer_target_for_filename,
)


_PANEL = "find_pockets"
_VIEWER_PRODUCING_KINDS = ("find_pockets",)


def _live_task_or_none():
    """Return ``(task, owner)`` for the in-flight find_pockets task, or
    ``(None, None)`` when nothing is running.

    ``owner`` is always ``"find_pockets"`` post-B2.1 (the pipeline task
    was removed); the tuple shape is kept for back-compat with call
    sites that destructure it.
    """
    fp_id = st.session_state.get("find_pockets_task_id")
    if fp_id:
        task = get_async_result(fp_id)
        if task is not None:
            return task, "find_pockets"
    return None, None


def _render_settings(session, is_editor: bool) -> None:
    mode_label = st.radio(
        "Input source",
        ["From trajectory (XTC + topology)", "From PDB ZIP archive"],
        horizontal=False,
        key="fp_mode",
        help=(
            "Trajectory mode extracts frames then detects pockets. "
            "PDB ZIP mode skips extraction when you already have structures."
        ),
    )
    mode_is_trajectory = mode_label.startswith("From trajectory")

    if mode_is_trajectory:
        st.file_uploader(
            "Trajectory (.xtc)",
            type=["xtc"],
            key="fp_xtc",
            help="Molecular dynamics trajectory file",
        )
        st.file_uploader(
            "Topology (.pdb, .gro)",
            type=["pdb", "gro"],
            key="fp_top",
            help="Reference topology / starting structure",
        )
        st.number_input(
            "Frame stride",
            min_value=1,
            value=10,
            key="fp_stride",
            help="Extract every Nth frame from the trajectory.",
        )
    else:
        st.file_uploader(
            "PDB structures (.zip)",
            type=["zip"],
            key="fp_zip",
            help="ZIP archive of .pdb files — extraction will be skipped.",
        )

    # B11.21: p2rank thread count is an .env knob (P2RANK_THREADS),
    # not user-facing — the "Threads" number_input was removed.

    disabled = not is_editor
    label = "Find Pockets" if is_editor else "Find Pockets (read-only)"
    if not st.button(
        label,
        type="primary",
        use_container_width=True,
        disabled=disabled,
        key="fp_submit",
    ):
        return

    _submit(mode_is_trajectory, session)


def _submit(mode_is_trajectory: bool, session) -> None:
    # Phase C C4: refuse early when the fast pool or this session's
    # fast-pool slots are saturated. Avoids spending an upload before
    # finding out we have nowhere to send the job.
    sess_id = getattr(session, "id", None) if session else None
    try:
        assert_submit_allowed("fast", sess_id)
    except PoolCapHit as e:
        render_pool_cap_error(e)
        return

    job_id = new_job_id("find_pockets")
    upload_dir = str(Config.UPLOAD_DIR)

    xtc_path = topology_path = pdb_input_dir = None

    if mode_is_trajectory:
        xtc_file = st.session_state.get("fp_xtc")
        topology_file = st.session_state.get("fp_top")
        if not xtc_file or not topology_file:
            st.error("Both trajectory and topology files are required.")
            return
        try:
            xtc_path = str(handle_file_upload_secure(
                xtc_file, job_id, "trajectory_", session_id=sess_id))
            topology_path = str(handle_file_upload_secure(
                topology_file, job_id, "topology_", session_id=sess_id))
        except RateLimitExceeded as e:
            st.error(f"Rate limit exceeded — wait {e.retry_after:.0f}s.")
            return
        except SecurityError as e:
            st.error(f"File upload failed: {e}")
            return
    else:
        pdb_zip = st.session_state.get("fp_zip")
        if not pdb_zip:
            st.error("Upload a ZIP archive containing PDB files.")
            return
        try:
            zip_path = handle_file_upload_secure(
                pdb_zip, job_id, "pdbs_", session_id=sess_id)
            FileValidator.validate_zip_file(Path(str(zip_path)))
        except SecurityError as e:
            st.error(f"ZIP upload failed: {e}")
            return

        pdb_input_dir = os.path.join(upload_dir, job_id, "extracted_pdbs")
        os.makedirs(pdb_input_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            if not any(name.lower().endswith(".pdb") for name in zf.namelist()):
                st.error(
                    "No PDB files in this ZIP. Make sure your archive contains "
                    "`.pdb` structures at any depth."
                )
                return
            zf.extractall(pdb_input_dir)
        pdb_count = sum(1 for _ in Path(pdb_input_dir).rglob("*.pdb"))
        if pdb_count == 0:
            st.error("No PDB files found after extracting the ZIP.")
            return
        # Frame-count cap: the Mol* viewer falls over silently above this,
        # even when MAX_VIEWER_BYTES is satisfied. Refuse pre-dispatch with
        # a clear message rather than letting the job run and produce an
        # unviewable trajectory.
        if pdb_count > Config.MAX_TRAJECTORY_FRAMES:
            st.error(
                f"This ZIP contains {pdb_count} PDB structures — over the "
                f"`MAX_TRAJECTORY_FRAMES = {Config.MAX_TRAJECTORY_FRAMES}` "
                f"limit. The viewer can't render this many frames "
                f"reliably. Pre-stride your structures (e.g. keep every "
                f"{max(2, pdb_count // Config.MAX_TRAJECTORY_FRAMES + 1)}th "
                f"file) or raise the cap in .env."
            )
            return

    try:
        check_task_rate_limit()
    except RateLimitExceeded as e:
        st.error(f"Task rate limit exceeded — wait {e.retry_after:.0f}s.")
        return

    register_session_job(job_id, "find_pockets")

    task = run_find_pockets_task.delay(
        job_id=job_id,
        xtc_file_path=xtc_path,
        topology_file_path=topology_path,
        pdb_input_dir=pdb_input_dir,
        stride=int(st.session_state.get("fp_stride") or 10),
        # B11.21: num_threads omitted — the task sources Config.P2RANK_THREADS.
    )
    st.session_state.find_pockets_job_id = job_id
    st.session_state.find_pockets_task_id = task.id
    st.session_state.find_pockets_status = "running"
    # B3.4: forensic record. Defensive — failure is logged, not raised.
    from client_ip import client_ip
    from db import audit
    audit.record(sess_id, client_ip(), "find_pockets_submit",
                 {"job_id": job_id, "mode": "trajectory" if mode_is_trajectory else "pdb_dir"})
    # B11.10: clear the force-settings flag — the new task IS the
    # transition we held the settings view to enable.
    st.session_state.pop("fp_force_settings", None)
    st.rerun()


@st.fragment(run_every="3s")
def _render_running_fragment() -> None:
    """Polls find_pockets task state every 3 s without re-rendering the whole page.

    Mirrors the docking panel's ``_docking_running_fragment``. Before
    this refactor, the running state used ``time.sleep(3); st.rerun()``
    inside ``render_running_progress`` — that pattern caused the entire
    analysis_app DOM to be APPENDED (faded) below itself on every tick
    because the new render started while the old one was still visible.

    Fragment scope means ``st.rerun()`` inside reruns *only this
    block*, leaving the header/viewer/jobs panel alone. On terminal
    states (SUCCESS / FAILURE / task vanished) we escalate to an
    app-scope rerun so the panel transitions to the results view.
    """
    task, owner = _live_task_or_none()
    if task is None:
        st.rerun(scope="app")  # task gone — fall through to results/settings
        return

    state = task.state
    if state == "FAILURE":
        job_id = st.session_state.get("find_pockets_job_id", "")
        render_failure(task, job_id, panel_prefix=owner)
        return

    if state == "SUCCESS":
        st.session_state.pop("find_pockets_task_id", None)
        st.session_state.find_pockets_status = "completed"
        st.rerun(scope="app")  # exit to results view
        return

    render_running_progress(task, "Working on pocket detection…", pool="fast")


def _render_results(latest_job: dict, is_editor: bool, session) -> None:
    results_job_id = latest_job.get("legacy_id") or ""
    try:
        results_job_id = FileValidator.validate_job_id(results_job_id)
    except SecurityError as e:
        st.error(f"Invalid job ID for results: {e}")
        return

    results_dir = str(Config.RESULTS_DIR)
    pockets_csv = os.path.join(results_dir, results_job_id, "pockets", "pockets.csv")

    header_l, header_r = st.columns([3, 1])
    with header_l:
        st.caption(f"Latest run: `{results_job_id}`")
    with header_r:
        if st.button(
            "Re-run",
            key="fp_rerun_from_results",
            disabled=not is_editor,
            use_container_width=True,
            help="Clear settings and submit a new find_pockets job.",
        ):
            # B11.10: also force the settings view. Clearing the
            # in-memory task ids alone wasn't enough — the DB still
            # has the completed job, so ``latest_job_of_kind`` would
            # find it on the next render and route back to results.
            for k in ("find_pockets_task_id", "find_pockets_job_id", "find_pockets_status"):
                st.session_state.pop(k, None)
            st.session_state["fp_force_settings"] = True
            st.rerun()

    if not os.path.exists(pockets_csv):
        st.warning(
            "Latest find_pockets job completed but pockets.csv is missing on disk — "
            "the session results may have been pruned."
        )
        return

    try:
        df_pockets = pd.read_csv(pockets_csv)
    except Exception as e:
        st.error(f"Could not read pockets.csv: {e}")
        return

    if "residues" in df_pockets.columns and df_pockets["residues"].dtype == object:
        df_pockets["num_residues"] = df_pockets["residues"].apply(
            lambda x: len(str(x).split()) if pd.notna(x) else 0
        )
    elif "residues" in df_pockets.columns:
        df_pockets["num_residues"] = df_pockets["residues"]
    else:
        df_pockets["num_residues"] = 0

    if len(df_pockets) == 0:
        st.warning("Detection completed but no pockets were found.")
        st.info(
            "Lower the stride to extract more frames, verify the trajectory/topology "
            "match, or inspect the error log in the Jobs panel."
        )
        return

    # Pocket annotations are derived by the viewer fragment in
    # ``analysis_app.py`` via ``derive_session_annotations``; the panel
    # no longer pushes them directly.

    # B11: compact one-line stats strip in place of the 2×2 ``st.metric``
    # grid that ate too much vertical space at the 50/50 column width.
    n_hc = int((df_pockets["probability"] >= 0.7).sum())
    st.markdown(
        f"**{len(df_pockets)} pockets** &nbsp;·&nbsp; "
        f"avg p={df_pockets['probability'].mean():.2f} &nbsp;·&nbsp; "
        f"{n_hc} high-confidence (≥ 0.7) &nbsp;·&nbsp; "
        f"best p={df_pockets['probability'].max():.2f}"
    )

    tab_table, tab_dist, tab_dl = st.tabs(["Pockets", "Distribution", "Downloads"])

    with tab_table:
        def _badge(p: float) -> str:
            if p >= 0.7:
                return "High"
            if p >= 0.4:
                return "Medium"
            return "Low"

        df_disp = df_pockets.sort_values("probability", ascending=False).copy()
        df_disp["Confidence"] = df_disp["probability"].apply(_badge)

        min_p = st.slider("Min probability", 0.0, 1.0, 0.0, 0.05, key="fp_minp")
        conf_pick = st.multiselect(
            "Confidence",
            options=["High", "Medium", "Low"],
            default=["High", "Medium", "Low"],
            key="fp_conf",
        )

        df_f = df_disp[df_disp["probability"] >= min_p]
        if conf_pick:
            df_f = df_f[df_f["Confidence"].isin(conf_pick)]

        cols = [
            c for c in ["Frame", "pocket_index", "probability", "num_residues", "Confidence"]
            if c in df_f.columns
        ]
        sel = st.dataframe(
            df_f[cols].reset_index(drop=True),
            use_container_width=True,
            height=320,
            on_select="rerun",
            selection_mode="multi-row",
            key="fp_pocket_table",
        )
        st.caption(
            f"Showing {len(df_f)} of {len(df_pockets)} pockets. "
            "Click a row to preview; ctrl/shift-click to multi-select for docking."
        )

        # B10: table selection drives the Mol* preview (first selected row).
        # B11.2: multi-row selection — the "Add N selected to docking" button
        # below sends the entire selection into the cross-stage bucket.
        rows = (sel.selection.get("rows") or []) if sel and getattr(sel, "selection", None) else []

        # Action row: bulk-add to the docking bucket.
        if rows:
            from panels import _docking_bucket as docking_bucket

            if st.button(
                f"Add {len(rows)} selected → docking",
                key="fp_add_to_docking",
                use_container_width=True,
                disabled=not is_editor,
                help="Pockets are added to the cross-stage docking selection.",
            ):
                rows_to_add = df_f.iloc[rows]
                entries = docking_bucket.from_pockets_df(
                    rows_to_add, source_job_id=results_job_id,
                )
                n_added = docking_bucket.add(entries)
                if n_added > 0:
                    st.success(
                        f"Added {n_added} pocket(s) to docking — see the bottom strip."
                    )
                else:
                    st.info(
                        "Those pockets are already in the docking selection."
                    )
        if rows:
            row_idx = rows[0]
            row = df_f.iloc[row_idx]
            residues_raw = row.get("residues") if "residues" in row else None
            if residues_raw is None or (
                isinstance(residues_raw, float) and pd.isna(residues_raw)
            ):
                # Fall back to df_pockets in case ``residues`` was dropped.
                fid = row.get("Frame_pocket_index", None)
                if fid is not None and "Frame_pocket_index" in df_pockets.columns:
                    src = df_pockets[df_pockets["Frame_pocket_index"] == fid]
                    if len(src):
                        residues_raw = src.iloc[0].get("residues")
            residue_list: list[str] = []
            if isinstance(residues_raw, str):
                residue_list = [tok for tok in residues_raw.split() if tok]

            from components.molstar_annotations import merge_annotations

            ann_key = f"viewer_annotations_{session.short_code}"
            ann_dict = st.session_state.setdefault(ann_key, {})
            label_parts = []
            if "pocket_index" in row and pd.notna(row.get("pocket_index")):
                label_parts.append(f"Pocket {int(row['pocket_index'])}")
            if "probability" in row and pd.notna(row.get("probability")):
                label_parts.append(f"p={float(row['probability']):.2f}")
            label = " · ".join(label_parts) or "Selected pocket"

            if len(residue_list) >= 3:
                merge_annotations(
                    ann_dict,
                    pockets=[{
                        "residues": residue_list,
                        "color": "#d4ff00",  # acid yellow — matches brutalist accent
                        "label": label,
                    }],
                    focus={"type": "pocket", "target": 0},
                )

                # Also jump the viewer to the trajectory frame where this
                # pocket was detected. The panel writes the logical PDB
                # *filename* into ``target_key``; the viewer fragment
                # resolves it via the stride manifest (overview snap if
                # the frame is in the strided set, single-frame focused
                # mode otherwise). We guard with ``last_select_key`` so
                # the frame doesn't get re-pushed every rerun while the
                # same pocket stays selected — that would fight the user
                # dragging the slider afterwards.
                file_name = row.get("File name") if "File name" in row else None
                norm_name = (
                    _normalise_pdb_filename(str(file_name)) if file_name else None
                )
                target = (
                    viewer_target_for_filename(results_job_id, norm_name)
                    if norm_name else None
                )
                from db.sessions import safe_bidi_short
                safe_short = safe_bidi_short(session.short_code)
                last_select_key = f"fp_last_pocket_select_{safe_short}"
                target_key = f"viewer_frame_target_{safe_short}"
                fingerprint = (
                    str(row.get("Frame_pocket_index", "")),
                    str(row.get("File name", "")),
                    str(row.get("pocket_index", "")),
                )
                if (
                    target is not None
                    and st.session_state.get(last_select_key) != fingerprint
                ):
                    st.session_state[last_select_key] = fingerprint
                    st.session_state[target_key] = target.filename
                    st.rerun()
            else:
                st.warning(
                    f"Pocket has {len(residue_list)} residues — surface needs ≥3 "
                    "to render a meaningful mesh."
                )
        else:
            # No selection — clear the single-row pocket annotation so the
            # viewer goes back to cartoon-only.
            from components.molstar_annotations import merge_annotations

            ann_key = f"viewer_annotations_{session.short_code}"
            ann_dict = st.session_state.setdefault(ann_key, {})
            if ann_dict.get("pockets") or ann_dict.get("focus"):
                merge_annotations(ann_dict, pockets=None, focus=None)

    with tab_dist:
        fig_h = px.histogram(
            df_pockets, x="probability", nbins=30,
            title="Probability distribution",
            color_discrete_sequence=["#000000"],
        )
        fig_h.update_layout(showlegend=False, height=260)
        st.plotly_chart(fig_h, use_container_width=True)

        fig_s = px.scatter(
            df_pockets, x="num_residues", y="probability",
            size="probability", color="probability",
            title="Probability vs size",
            labels={"num_residues": "Residues", "probability": "Probability"},
            color_continuous_scale="Greys",
        )
        fig_s.update_layout(height=260)
        st.plotly_chart(fig_s, use_container_width=True)

    with tab_dl:
        st.download_button(
            "pockets.csv",
            data=df_pockets.to_csv(index=False),
            file_name=f"pockets_{results_job_id}.csv",
            mime="text/csv",
            use_container_width=True,
        )

        df_hc = df_pockets[df_pockets["probability"] >= 0.7]
        if len(df_hc) > 0:
            st.download_button(
                "high-confidence subset",
                data=df_hc.to_csv(index=False),
                file_name=f"high_confidence_pockets_{results_job_id}.csv",
                mime="text/csv",
                use_container_width=True,
            )

        pockets_dir = os.path.join(results_dir, results_job_id, "pockets")
        pdbs_dir = os.path.join(results_dir, results_job_id, "pdbs")
        zip_path = os.path.join(pockets_dir, "pockets_pdbs.zip")
        if st.button(
            "Generate PDB archive",
            use_container_width=True,
            key="fp_genzip",
            disabled=not is_editor,
        ):
            if os.path.isdir(pdbs_dir):
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for p in Path(pdbs_dir).glob("*.pdb"):
                        zf.write(p, p.name)
                st.success("Archive created.")
            else:
                st.warning("No /pdbs directory for this job.")
        if os.path.exists(zip_path):
            with open(zip_path, "rb") as f:
                st.download_button(
                    "PDB archive (ZIP)",
                    data=f.read(),
                    file_name=f"pockets_pdbs_{results_job_id}.zip",
                    mime="application/zip",
                    use_container_width=True,
                )

    # B11: forward-momentum CTA. After pockets are detected, the next
    # natural step is clustering. Disabled in read-only sessions or when
    # the pockets table is empty (handled by the earlier early-return).
    st.divider()
    if st.button(
        "Next: Cluster →",
        type="primary",
        use_container_width=True,
        disabled=not is_editor,
        key="fp_next_cluster",
        help="Switch to the Cluster panel to group these pockets across frames.",
    ):
        # B11.1: write to the pending key, not ``active_stage`` directly.
        # The segmented_control with key="active_stage" has already
        # rendered this pass; Streamlit blocks late writes to widget
        # keys. ``analysis_app.render_analysis_app`` drains
        # ``pending_active_stage`` BEFORE instantiating the widget on
        # the next pass.
        st.session_state["pending_active_stage"] = "Cluster"
        st.rerun()


def render(session, is_editor: bool) -> None:
    """Stage-panel entry point dispatched from ``analysis_app.py``."""
    task, _owner = _live_task_or_none()
    if task is not None:
        _render_running_fragment()
        return

    # B11.10: the Re-run button sets this flag so the user lands back
    # on the settings form even though the DB still has a completed
    # job. Cleared automatically when a fresh task starts (``_submit``
    # writes ``find_pockets_task_id``, which the running-state branch
    # picks up on the next render).
    if st.session_state.get("fp_force_settings"):
        _render_settings(session, is_editor)
        return

    # B11.20: a Jobs-list click can pin a specific (possibly non-latest)
    # find_pockets job via fp_view_job_id; fall back to the latest run
    # when unset or stale.
    view_id = st.session_state.get("fp_view_job_id")
    latest = job_by_legacy_id(session.id, view_id) if view_id else None
    if latest is None:
        latest = latest_job_of_kind(session.id, _VIEWER_PRODUCING_KINDS)
    if latest is not None:
        _render_results(latest, is_editor, session)
        return

    _render_settings(session, is_editor)
