"""Docking stage panel (v2 analysis app).

The heaviest of the three panels: settings demand a cluster job, a
selection of representative PDBs to dock against, a ligand library, a
binding-site box, and smina knobs. Results give the per-pair best pose
table with an inline py3Dmol viewer (Mol* annotation overlays land in
B5 and will replace this viewer block).
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

from config import Config
from rate_limiter import RateLimitExceeded, check_task_rate_limit
from security import FileValidator, SecurityError
from session_routes import register_session_job
from tasks import run_docking_task, _count_sdf_molecules

from panels._shared import (
    get_async_result,
    job_by_legacy_id,
    jobs_of_kind,
    latest_job_of_kind,
    new_job_id,
    pipeline_in_flight,
    render_failure,
    render_running_progress,
)


_PANEL = "docking"
_RESULTS_KINDS = ("docking",)
_SOURCE_KINDS = ("cluster", "pipeline")


def _fmt_duration(seconds: float) -> str:
    """Compact human duration: ``45s`` / ``3m 20s`` / ``1h 5m``."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


@st.cache_data(show_spinner="Packaging docking results…")
def _results_zip_cached(output_dir: str, job_id: str):
    """Build (and cache) the all-results ZIP for a completed docking job.

    B11.21: bundles the per-pose ``*.sdf`` files + the full / best-poses
    CSVs. Excludes ``smina_debug.log`` (large, low value). Files are
    added smallest-first until ``Config.MAX_DOWNLOAD_ZIP_SIZE`` would be
    exceeded; the rest are reported as omitted. Cached on
    ``(output_dir, job_id)`` — a completed job's results are static, so
    the archive is built once. Returns ``(zip_bytes, omitted)``.
    """
    cap = Config.MAX_DOWNLOAD_ZIP_SIZE
    csv_path = os.path.join(output_dir, "docking_results.csv")
    full_csv = best_csv = ""
    try:
        df = pd.read_csv(csv_path)
        full_csv = df.to_csv(index=False)
        df_best = df.loc[
            df.groupby(["ligand", "receptor"])["affinity (kcal/mol)"].idxmin()
        ]
        best_csv = df_best.to_csv(index=False)
    except Exception:  # noqa: BLE001 — degrade to SDFs-only on a bad CSV
        pass

    candidates: list[tuple[str, str, int]] = []
    for root, _dirs, files in os.walk(output_dir):
        for fn in files:
            if not fn.endswith(".sdf"):
                continue
            fp = os.path.join(root, fn)
            try:
                size = os.path.getsize(fp)
            except OSError:
                continue
            candidates.append((fp, os.path.relpath(fp, output_dir), size))
    candidates.sort(key=lambda c: c[2])  # smallest-first

    omitted: list[str] = []
    running = len(full_csv) + len(best_csv)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if full_csv:
            zf.writestr(f"docking_results_{job_id}.csv", full_csv)
        if best_csv:
            zf.writestr(f"docking_best_poses_{job_id}.csv", best_csv)
        for fp, arc, size in candidates:
            if running + size > cap:
                omitted.append(arc)
                continue
            try:
                zf.write(fp, arcname=arc)
                running += size
            except OSError:
                omitted.append(arc)
    return buf.getvalue(), omitted


def _live_task_or_none():
    dock_id = st.session_state.get("docking_task_id")
    if dock_id:
        task = get_async_result(dock_id)
        if task is not None:
            return task
    return None


def _render_settings(session, is_editor: bool) -> None:
    pipeline_busy = pipeline_in_flight()
    if pipeline_busy:
        st.warning(
            "Pipeline run in progress — wait for it to finish before "
            "starting a docking job."
        )

    # B11.2: docking source is the cross-stage pocket bucket
    # (``st.session_state["docking_selected_pockets"]``) populated by
    # the Pockets / Cluster panels. The old cluster-job selectbox + PDB
    # multiselect is gone — the bucket carries enough information per
    # pocket to dock directly.
    from panels import _docking_bucket as docking_bucket

    bucket = docking_bucket.current()
    if not bucket:
        st.info(
            "No pockets selected for docking yet. Open the **Pockets** or "
            "**Cluster** panel and use *Add selected → docking* (or the "
            "*Add all cluster representatives* preset) to populate the bucket."
        )
        return

    # Display the bucket as a read-only-ish table.
    df_bucket = pd.DataFrame(bucket)
    visible_cols = [c for c in (
        "cluster", "Frame", "pocket_index", "probability",
    ) if c in df_bucket.columns]
    st.markdown(f"**{len(bucket)} pocket(s) staged for docking**")
    bucket_sel = st.dataframe(
        df_bucket[visible_cols],
        use_container_width=True,
        height=min(260, 60 + 28 * len(bucket)),
        on_select="rerun",
        selection_mode="multi-row",
        key="docking_bucket_table",
    )
    sel_rows = (
        (bucket_sel.selection.get("rows") or [])
        if bucket_sel and getattr(bucket_sel, "selection", None)
        else []
    )
    btn_cols = st.columns([2, 2, 3])
    if btn_cols[0].button(
        f"Remove {len(sel_rows)} selected" if sel_rows else "Remove",
        disabled=(not is_editor) or not sel_rows,
        use_container_width=True,
        key="docking_bucket_remove",
    ):
        keys_to_drop = [
            (df_bucket.iloc[i]["source_job_id"], df_bucket.iloc[i]["Frame_pocket_index"])
            for i in sel_rows
        ]
        docking_bucket.remove(keys_to_drop)
        st.rerun()
    if btn_cols[1].button(
        "Clear all",
        disabled=not is_editor,
        use_container_width=True,
        key="docking_bucket_clear",
    ):
        docking_bucket.clear()
        st.rerun()

    if len(bucket) > Config.MAX_DOCKING_PDBS:
        st.warning(
            f"{len(bucket)} pockets selected — the docking task will cap "
            f"at {Config.MAX_DOCKING_PDBS} (highest probability kept)."
        )

    # Ligand uploader.
    st.markdown("**Ligand library**")
    uploaded_files = st.file_uploader(
        "Ligand files (PDBQT / SDF / PDB / ZIP)",
        type=["pdbqt", "sdf", "pdb", "zip"],
        accept_multiple_files=True,
        key="docking_ligand_uploads",
        help="SDF and PDB are auto-split into one PDBQT per molecule via OpenBabel.",
    )

    # B11.16: Generate-3D is an OpenBabel preprocessing step, not a smina
    # option — surface it prominently outside the smina expander.
    st.checkbox(
        "Generate 3D coordinates for ligands",
        value=False,
        key="docking_gen_3d",
        help=(
            "Run OpenBabel `--gen3d` during prep. Off is correct for "
            "most curated libraries (e-Drug3D, ZINC 3D, etc.). Turn on "
            "only if your SDF/PDB inputs are 2D — adds minutes."
        ),
    )

    # B11.16: drop the X/Y/Z box sliders + "Auto-size from cluster X"
    # button — the task now computes a per-pocket box from each
    # pocket's residue bounding cloud. Drop the smina-path text input
    # too — the conda env's smina is always on PATH.
    with st.expander("smina parameters"):
        st.selectbox(
            "Scoring function",
            options=["vinardo", "vina", "ad4_scoring", "dkoes_scoring"],
            index=0,
            key="docking_scoring",
            help=(
                "vinardo: re-trained Vina, generally best for proteins.\n"
                "vina: AutoDock Vina default.\n"
                "ad4_scoring / dkoes_scoring: alternatives bundled with smina."
            ),
        )
        st.slider("Number of poses", 1, 50, 10, key="docking_num_poses")
        # B11.21: exhaustiveness is a fixed .env knob (DOCKING_EXHAUSTIVENESS),
        # not user-facing — the slider was removed.
        st.slider("pH (protonation)", 4.0, 10.0, 7.4, step=0.1, key="docking_ph")

    disabled = (
        (not is_editor)
        or pipeline_busy
        or not uploaded_files
    )
    if not st.button(
        "Dock",
        type="primary",
        use_container_width=True,
        disabled=disabled,
        key="docking_submit",
    ):
        if not uploaded_files:
            st.caption("Upload at least one ligand.")
        return

    _submit(df_bucket, uploaded_files)


def _prepare_ligand_dir(job_id: str, uploaded_files) -> list[str]:
    """Save uploaded ligand files to ``uploads/ligands_<job>/`` as-is.

    B11.12: conversion (obabel) moved into ``tasks.run_docking_task`` so
    progress shows in the task's progress bar. This helper just
    materialises uploaded bytes onto disk and unzips ZIPs. Accepts
    PDBQT, SDF, PDB, or ZIP. Returns a list of every saved input file
    so the caller can detect "empty upload" before kicking off the task.
    """
    upload_dir = str(Config.UPLOAD_DIR)
    ligand_dir = os.path.join(upload_dir, f"ligands_{job_id}")
    os.makedirs(ligand_dir, exist_ok=True)

    saved: list[str] = []
    for uf in uploaded_files:
        if uf.name.endswith(".zip"):
            zip_path = Path(ligand_dir) / uf.name
            zip_path.write_bytes(uf.getbuffer())
            try:
                FileValidator.validate_zip_file(zip_path)
            except SecurityError as e:
                st.error(f"ZIP rejected ({uf.name}): {e}")
                continue
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(ligand_dir)
            try:
                zip_path.unlink()
            except OSError:
                pass
            for root, _dirs, files in os.walk(ligand_dir):
                for f in files:
                    if f.lower().endswith((".pdbqt", ".sdf", ".pdb")):
                        saved.append(os.path.join(root, f))
        else:
            try:
                safe = FileValidator.validate_filename(uf.name)
            except SecurityError as e:
                st.error(f"Rejected `{uf.name}`: {e}")
                continue
            if not safe.lower().endswith((".pdbqt", ".sdf", ".pdb")):
                st.error(
                    f"Rejected `{safe}`: only .pdbqt / .sdf / .pdb / .zip accepted."
                )
                continue
            path = os.path.join(ligand_dir, safe)
            with open(path, "wb") as fh:
                fh.write(uf.getbuffer())
            saved.append(path)
    return saved


def _submit(df_bucket: pd.DataFrame, uploaded_files):
    """B11.2: submit a docking job from the cross-stage pocket bucket.

    The bucket DataFrame carries ``File name``, ``residues``,
    ``probability``, ``Frame``, ``pocket_index``, ``cluster``, and
    ``source_job_id`` per pocket. We trim to ``MAX_DOCKING_PDBS``
    (highest probability kept), write the trimmed set to a temp CSV
    that the docking task reads (it only needs ``File name`` +
    ``residues``), and pass that as ``cluster_representatives_csv``
    for backwards compatibility.
    """
    try:
        check_task_rate_limit()
    except RateLimitExceeded as e:
        st.error(f"Task rate limit exceeded — wait {e.retry_after:.0f}s.")
        return

    job_id = new_job_id("docking")
    upload_dir = str(Config.UPLOAD_DIR)

    # B11.12: save ligands; conversion + cap happens in the task so
    # obabel progress lands in the docking progress bar.
    saved_paths = _prepare_ligand_dir(job_id, uploaded_files)
    if not saved_paths:
        st.error("No ligand files saved. Check the errors above.")
        return

    # Trim to the docking cap by probability descending.
    df_bucket = df_bucket.sort_values("probability", ascending=False, kind="mergesort")
    if len(df_bucket) > Config.MAX_DOCKING_PDBS:
        df_bucket = df_bucket.head(Config.MAX_DOCKING_PDBS)

    # B11.21: enforce the per-run ligand×pocket pairs budget. Molecule
    # count comes from the saved SDFs (PDB/PDBQT count as 1 each); the
    # pocket count is the post-trim bucket size.
    n_pockets = len(df_bucket)
    n_molecules = sum(_count_sdf_molecules(p) for p in saved_paths)
    n_pairs = n_molecules * n_pockets
    if n_pockets and n_pairs > Config.DOCKING_MAX_PAIRS:
        max_mols = max(1, Config.DOCKING_MAX_PAIRS // n_pockets)
        st.error(
            f"Docking budget exceeded: {n_molecules} molecule(s) × "
            f"{n_pockets} pocket(s) = {n_pairs:,} pairs, over the "
            f"{Config.DOCKING_MAX_PAIRS:,}-pair limit. Upload ≤ {max_mols} "
            f"molecule(s) or select fewer pockets."
        )
        # Nothing dispatched — drop the saved ligand dir.
        shutil.rmtree(os.path.join(upload_dir, f"ligands_{job_id}"), ignore_errors=True)
        return

    filtered_reps_file = os.path.join(upload_dir, f"filtered_reps_{job_id}.csv")
    os.makedirs(os.path.dirname(filtered_reps_file), exist_ok=True)
    df_bucket.to_csv(filtered_reps_file, index=False)

    # Resolve the source PDB directory from the bucket's source_job_id
    # (the find_pockets / pipeline job that owns the pdbs/ folder).
    pdb_source_dir = None
    if "source_job_id" in df_bucket.columns:
        sources = df_bucket["source_job_id"].dropna().unique().tolist()
        for src in sources:
            try:
                src_validated = FileValidator.validate_job_id(str(src))
            except SecurityError:
                continue
            candidate = os.path.join(str(Config.RESULTS_DIR), src_validated, "pdbs")
            if os.path.exists(candidate):
                pdb_source_dir = candidate
                break
    if pdb_source_dir is None:
        # Legacy fallback — last-known PDB-producing job in results/.
        results_dir = str(Config.RESULTS_DIR)
        for dirname in sorted(os.listdir(results_dir), reverse=True):
            if dirname.startswith(("extract_", "find_pockets_", "pipeline_")):
                candidate = os.path.join(results_dir, dirname, "pdbs")
                if os.path.exists(candidate):
                    pdb_source_dir = candidate
                    break

    register_session_job(job_id, "docking")

    # B11.16: snapshot the bucket NOW so the running-state grid can
    # render the right columns even if the user clears the bucket
    # afterwards. We also stash the result-dir path so the running
    # state can find the partial docking_results.csv.
    st.session_state["docking_running_bucket"] = df_bucket.to_dict("records")
    st.session_state["docking_running_results_dir"] = os.path.join(
        str(Config.RESULTS_DIR), f"dock_{job_id}",
    )

    ligand_dir = os.path.join(upload_dir, f"ligands_{job_id}")
    task = run_docking_task.delay(
        cluster_representatives_csv=filtered_reps_file,
        ligand_folder=ligand_dir,
        job_id=job_id,
        num_poses=int(st.session_state.get("docking_num_poses", 10)),
        exhaustiveness=Config.DOCKING_EXHAUSTIVENESS,
        ph_value=float(st.session_state.get("docking_ph", 7.4)),
        pdb_source_dir=pdb_source_dir,
        gen_3d=bool(st.session_state.get("docking_gen_3d", False)),
        scoring_function=str(st.session_state.get("docking_scoring", "vinardo")),
    )
    st.session_state.docking_job_id = job_id
    st.session_state.docking_task_id = task.id
    st.session_state.docking_status = "running"
    # B11.10: clear the force-settings flag — fresh task is the
    # transition we held the settings view to enable.
    st.session_state.pop("docking_force_settings", None)
    st.rerun()


def _pocket_label(rec: dict) -> str:
    """Compact receptor label: ``C0_F2085`` (cluster + frame).

    B11.19: dropped the probability term — the docking-grid column
    headers and the receptor slider have very little horizontal room.
    The ``C{cluster}_`` prefix is omitted when the pocket was staged
    pre-clustering (``cluster`` is ``None``). Falls back to
    ``Frame_pocket_index`` then a generic token when no frame number
    is available.
    """
    cluster = rec.get("cluster")
    c_part = ""
    if cluster is not None and not (isinstance(cluster, float) and pd.isna(cluster)):
        try:
            c_part = f"C{int(cluster)}_"
        except (TypeError, ValueError):
            c_part = ""
    frame = rec.get("Frame")
    if frame is not None and not (isinstance(frame, float) and pd.isna(frame)):
        try:
            return f"{c_part}F{int(float(frame))}"
        except (TypeError, ValueError):
            pass
    fpi = rec.get("Frame_pocket_index")
    if fpi:
        return f"{c_part}{fpi}"
    return c_part.rstrip("_") or "pocket"


def _receptor_basename(file_name) -> str:
    """Map a bucket ``File name`` to the receptor basename smina records.

    The task strips the p2rank ``_predictions`` suffix and ensures
    ``.pdb`` — match that here so the live grid can join the partial
    results CSV's ``receptor`` column back to the bucket pockets.
    """
    name = str(file_name or "")
    if name.endswith("_predictions"):
        name = name[: -len("_predictions")]
    if not name.endswith(".pdb"):
        name = name + ".pdb"
    return name


def _build_receptor_columns(bucket_records: list[dict]) -> list[dict]:
    """Ordered receptor columns from a bucket snapshot.

    Each entry: ``{label, receptor (basename), rec (bucket dict)}``.
    Labels are deduped with a numeric suffix on collision.
    """
    cols = [
        {
            "label": _pocket_label(rec),
            "receptor": _receptor_basename(rec.get("File name", "")),
            "rec": rec,
        }
        for rec in bucket_records
    ]
    seen: dict[str, int] = {}
    for c in cols:
        base = c["label"]
        if base in seen:
            seen[base] += 1
            c["label"] = f"{base} #{seen[base]}"
        else:
            seen[base] = 1
    return cols


def _render_docking_dashboard(
    session,
    is_editor: bool,
    results_csv_path: str,
    bucket_records: list[dict],
    status_msg: str,
) -> None:
    """B11.18: the single docking view — used by both the running
    fragment and the completed results state.

    Renders a ligand × receptor score grid, ranked by each ligand's
    mean affinity across receptors (best average on top). Clicking a
    ligand row pushes that ligand's best pose into the Mol* viewer
    and snaps the active receptor to wherever it scored best. The
    receptor switcher itself lives in the left-column viewer fragment
    (``analysis_app._viewer_fragment``) — this function publishes the
    ordered receptor list into session_state for it to consume.
    """
    from panels._shared import frame_index_for_filename
    from viewer_pipeline import frame_number_from_filename

    safe_short = session.short_code.replace("__", "_")
    aff_col = "affinity (kcal/mol)"

    pockets = _build_receptor_columns(bucket_records)

    df = None
    if results_csv_path and os.path.exists(results_csv_path):
        try:
            df = pd.read_csv(results_csv_path)
        except Exception:
            df = None
    have_df = (
        df is not None and not df.empty
        and {"ligand", "receptor", aff_col}.issubset(df.columns)
    )

    # Fallback: no bucket snapshot (fresh tab on a completed session,
    # filtered_reps CSV also pruned) — rebuild minimal columns straight
    # from the results CSV. Label each by its trailing frame number
    # (the receptor basename is always ``<xtc_base>_<N>.pdb``); degrade
    # to a truncated basename for custom names with no frame index.
    if not pockets and have_df:
        for r in sorted(df["receptor"].astype(str).unique()):
            fn = frame_number_from_filename(r)
            label = f"F{fn}" if fn else (r[:18] + "…" if len(r) > 19 else r)
            pockets.append({"label": label, "receptor": r, "rec": {}})

    # Resolve the session's pdb-producing job once — it owns the
    # ``pdbs/`` dir that frame_index_for_filename globs. Authoritative
    # for every receptor, including the bucket-less fallback path where
    # the per-rec source_job_id is absent.
    src_job = latest_job_of_kind(session.id, ("find_pockets", "pipeline"))
    pdb_source_job_id = (src_job or {}).get("legacy_id") or ""

    # Publish the receptor list for the viewer fragment's slider — even
    # before any scores exist, so the slider appears immediately.
    recv_key = f"docking_receptors_{safe_short}"
    had_receptors = recv_key in st.session_state
    if pockets:
        st.session_state[recv_key] = [
            {
                "label": p["label"],
                "receptor": p["receptor"],
                "frame_model_index": frame_index_for_filename(
                    pdb_source_job_id,
                    str(p["rec"].get("File name", "")) or p["receptor"],
                ),
            }
            for p in pockets
        ]

    if not have_df or not pockets:
        st.info(
            "Docking in progress — scores will appear here as ligand-receptor "
            "pairs complete."
        )
        st.caption(status_msg)
        if pockets and not had_receptors:
            st.rerun()  # let the viewer fragment pick up the slider now
        return

    pocket_labels = [p["label"] for p in pockets]

    # Best pose per (ligand, receptor).
    grp = df.loc[df.groupby(["ligand", "receptor"])[aff_col].idxmin()]
    best: dict[tuple[str, str], float] = {}
    for _, row in grp.iterrows():
        best[(str(row["ligand"]), str(row["receptor"]))] = float(row[aff_col])
    ligands = sorted(df["ligand"].astype(str).unique())

    # Wide grid: rows = ligands, columns = receptor labels.
    grid = pd.DataFrame(index=ligands, columns=pocket_labels, dtype="float64")
    for lig in ligands:
        for p in pockets:
            grid.loc[lig, p["label"]] = best.get((lig, p["receptor"]), float("nan"))

    # Rank rows by mean affinity across scored receptors — most
    # negative average on top.
    grid["__avg__"] = grid.mean(axis=1, skipna=True)
    grid = grid.sort_values("__avg__", ascending=True, na_position="last")
    ranked_ligands = list(grid.index)
    grid = grid.drop(columns="__avg__")

    # Active receptor column — the index the viewer fragment's slider
    # last set. Clamp in case the bucket changed under us.
    recv_idx_key = f"docking_active_receptor_idx_{safe_short}"
    active_idx = max(0, min(int(st.session_state.get(recv_idx_key, 0)), len(pockets) - 1))
    active_label = pocket_labels[active_idx]

    def _highlight_active(col):
        hit = col.name == active_label
        return ["background-color: #d4ff00" if hit else "" for _ in col]

    # B11.21: show real molecule names (the `ligand_name` column) as the
    # grid row labels. `ranked_ligands` / `grid.index` stay as the PDBQT
    # stems for the pose lookups below — row selection is positional, so
    # the display relabel doesn't disturb it.
    name_map: dict[str, str] = {}
    if "ligand_name" in df.columns:
        name_map = {
            str(r["ligand"]): str(r["ligand_name"])
            for _, r in df[["ligand", "ligand_name"]].drop_duplicates("ligand").iterrows()
            if str(r["ligand_name"]).strip()
        }
    display_grid = grid.rename(index=lambda s: name_map.get(s, s))
    styled = display_grid.style.apply(_highlight_active, axis=0).format("{:.2f}", na_rep="—")
    grid_sel = st.dataframe(
        styled,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row",
        key="docking_grid_table",
    )
    sel_rows = (
        (grid_sel.selection.get("rows") or [])
        if grid_sel and getattr(grid_sel, "selection", None)
        else []
    )

    n_done = len(best)
    n_total = len(ligands) * len(pockets)
    st.caption(f"{status_msg} · {n_done}/{n_total} ligand-receptor pairs scored")

    # Selected ligand: the clicked row, else the top-ranked ligand.
    # B11.19: a row click selects the ligand only — it no longer snaps
    # the active receptor to that ligand's best score. The receptor
    # stays wherever the slider put it; the pose shown is always
    # (selected_ligand, active_pocket).
    if sel_rows:
        selected_ligand = ranked_ligands[sel_rows[0]]
    else:
        selected_ligand = ranked_ligands[0] if ranked_ligands else None

    if selected_ligand is None:
        if pockets and not had_receptors:
            st.rerun()
        return

    active_pocket = pockets[active_idx]
    pose_rows = df[
        (df["ligand"].astype(str) == selected_ligand)
        & (df["receptor"].astype(str) == active_pocket["receptor"])
    ]

    # Debounce: only re-push the annotation + rerun when the
    # (ligand, receptor) selection actually changes.
    fingerprint = (selected_ligand, active_label)
    last_key = f"docking_grid_last_fp_{safe_short}"
    changed = st.session_state.get(last_key) != fingerprint

    if not pose_rows.empty:
        pose = pose_rows.loc[pose_rows[aff_col].idxmin()].to_dict()
        from components.molstar_annotations import (
            ligand_pose_annotation_from_pose,
            merge_annotations,
        )

        ann_key = f"viewer_annotations_{safe_short}"
        ann_dict = st.session_state.setdefault(ann_key, {})
        ligand_ann = ligand_pose_annotation_from_pose(pose)
        if ligand_ann is not None:
            merge_annotations(
                ann_dict,
                ligand_pose=ligand_ann,
                focus={"type": "ligand", "target": None},
            )

    if changed or not had_receptors:
        st.session_state[last_key] = fingerprint
        st.rerun()  # app scope — propagate to the viewer fragment


@st.fragment(run_every="3s")
def _docking_running_fragment(session, is_editor: bool) -> None:
    """B11.18: poll docking progress in a fragment so the page does
    NOT grey out — the user keeps interacting with the score grid +
    receptor slider while smina runs. Auto-reruns every 3 s
    (fragment-scoped); interaction handlers in the dashboard escalate
    to an app-scope rerun when they need the viewer fragment to react.
    """
    task = _live_task_or_none()
    if task is None:
        st.rerun(scope="app")  # task vanished — re-dispatch
        return

    state = task.state
    if state == "FAILURE":
        render_failure(
            task,
            st.session_state.get("docking_job_id", ""),
            panel_prefix=_PANEL,
        )
        return
    if state == "SUCCESS":
        st.session_state.pop("docking_task_id", None)
        st.session_state.docking_status = "completed"
        # B11.18: keep docking_running_bucket + results_dir — the
        # results view reuses them for the same dashboard.
        st.rerun(scope="app")  # exit to the results view
        return

    info = task.info if isinstance(task.info, dict) else {}
    progress = int(info.get("progress") or 0)
    pairs_done = int(info.get("pairs_done", 0) or 0)
    pairs_total = int(info.get("pairs_total", 0) or 0)
    elapsed = float(info.get("elapsed", 0) or 0)
    if state == "PENDING":
        st.info("Queued — waiting for a worker to pick this up.")
        st.progress(0)
        from panels._shared import queue_depth_ahead
        ahead = queue_depth_ahead()
        if ahead is not None and ahead > 1:
            st.caption(f"~{ahead - 1} job(s) ahead of this one in the queue.")
    else:
        # B11.21: real ETA from the pairs-done fraction (docking has a
        # genuine progress fraction, unlike the find_pockets log-ramp).
        text = f"{progress}% · docking"
        if pairs_done > 0 and pairs_total > 0 and elapsed > 0:
            frac = pairs_done / pairs_total
            eta = elapsed / frac * (1.0 - frac)
            text += f" · ~{_fmt_duration(eta)} remaining"
        elif elapsed > 0:
            text += f" · {int(elapsed)}s elapsed (preparing receptors)"
        st.progress(progress / 100, text=text)

    bucket = st.session_state.get("docking_running_bucket", [])
    results_dir = st.session_state.get("docking_running_results_dir", "")
    partial_csv = (
        os.path.join(results_dir, "docking_results.csv") if results_dir else ""
    )
    _render_docking_dashboard(
        session, is_editor, partial_csv, bucket,
        f"⏳ Docking — {pairs_done}/{pairs_total} pairs",
    )


def _render_running(task, session, is_editor: bool) -> None:
    """Running-state entry point — delegates to the polling fragment.

    The fragment owns the FAILURE / SUCCESS handling + the live
    dashboard; ``task`` is re-fetched inside it via
    ``_live_task_or_none``.
    """
    _docking_running_fragment(session, is_editor)


def _render_results(latest_job: dict, is_editor: bool, session) -> None:
    results_job_id = latest_job.get("legacy_id") or ""
    try:
        results_job_id = FileValidator.validate_job_id(results_job_id)
    except SecurityError as e:
        st.error(f"Invalid job ID for results: {e}")
        return

    safe_short = session.short_code.replace("__", "_")
    info = latest_job.get("result_info") or {}
    results_file = info.get("docking_results_file") if isinstance(info, dict) else None

    header_l, header_r = st.columns([3, 1])
    with header_l:
        # B11.20: run-history selector — past docking runs stay reachable
        # instead of being shadowed by the latest. Selecting one pins it
        # via docking_view_job_id (honored by render()). No widget key —
        # the ``index`` (derived from the job actually being rendered) is
        # the single source of truth, so clearing docking_view_job_id
        # elsewhere can't be undone by a stale widget value.
        runs = jobs_of_kind(session.id, _RESULTS_KINDS)
        if len(runs) > 1:
            run_ids = [r.get("legacy_id") or "" for r in runs]

            def _run_label(lid: str) -> str:
                r = next((x for x in runs if x.get("legacy_id") == lid), {})
                ri = r.get("result_info") or {}
                bits = [lid or "?"]
                if isinstance(ri, dict) and ri.get("pairs_total"):
                    bits.append(f"{ri['pairs_total']} pairs")
                ts = (r.get("last_updated") or "")[:16].replace("T", " ")
                if ts:
                    bits.append(ts)
                return " · ".join(bits)

            try:
                cur_idx = run_ids.index(results_job_id)
            except ValueError:
                cur_idx = 0
            picked = st.selectbox(
                "Docking run",
                options=run_ids,
                index=cur_idx,
                format_func=_run_label,
                label_visibility="collapsed",
            )
            if picked and picked != results_job_id:
                st.session_state["docking_view_job_id"] = picked
                st.rerun()
        else:
            st.caption(f"Latest run: `{results_job_id}`")
    with header_r:
        if st.button(
            "New docking run",
            key="docking_new_run",
            disabled=not is_editor,
            use_container_width=True,
        ):
            # B11.10: see panels/find_pockets.py Re-run handler. B11.18:
            # also drop the dashboard + receptor-slider state so a
            # fresh dock starts clean. B11.20: drop docking_view_job_id
            # so the run selector snaps to the new run after submit.
            for k in (
                "docking_task_id", "docking_job_id", "docking_status",
                "docking_running_bucket", "docking_running_results_dir",
                "docking_view_job_id",
                f"docking_receptors_{safe_short}",
                f"docking_active_receptor_idx_{safe_short}",
                f"docking_recv_propagated_{safe_short}",
                f"docking_grid_last_fp_{safe_short}",
            ):
                st.session_state.pop(k, None)
            # B11.20: pre-load the previous run's pockets so the setup
            # form is ready to dock even when this tab's bucket is empty
            # (e.g. the session was reopened in a fresh tab). The
            # filtered_reps CSV columns match the bucket-entry shape;
            # add() dedups, so it's a no-op when the bucket is populated.
            from panels import _docking_bucket as docking_bucket
            if not docking_bucket.current():
                reps_csv = os.path.join(
                    str(Config.UPLOAD_DIR), f"filtered_reps_{results_job_id}.csv"
                )
                if os.path.exists(reps_csv):
                    try:
                        docking_bucket.add(pd.read_csv(reps_csv).to_dict("records"))
                    except Exception:
                        pass
            st.session_state["docking_force_settings"] = True
            st.rerun()

    from failure_view import (
        render_ligand_conversion_callout,
        render_pair_failures_callout,
    )

    render_pair_failures_callout(info if isinstance(info, dict) else {}, results_job_id)
    render_ligand_conversion_callout(info if isinstance(info, dict) else {}, results_job_id)

    if not results_file or not os.path.exists(str(results_file)):
        st.warning("Docking results CSV is missing on disk — results may have been pruned.")
        return

    pairs_total = info.get("pairs_total") if isinstance(info, dict) else None
    pairs_done = info.get("pairs_done", pairs_total) if isinstance(info, dict) else None
    status_msg = "✅ Docking complete"
    if pairs_total:
        status_msg = f"✅ Docking complete — {pairs_done}/{pairs_total} pairs"

    bucket = st.session_state.get("docking_running_bucket", [])
    if not bucket:
        # Fresh tab / refreshed session — session_state lost. _submit
        # persisted the exact bucket snapshot to disk; reload it so
        # receptor labels + frame resolution still work.
        reps_csv = os.path.join(
            str(Config.UPLOAD_DIR), f"filtered_reps_{results_job_id}.csv"
        )
        if os.path.exists(reps_csv):
            try:
                bucket = pd.read_csv(reps_csv).to_dict("records")
            except Exception:
                bucket = []
    _render_docking_dashboard(session, is_editor, str(results_file), bucket, status_msg)

    with st.expander("Downloads"):
        try:
            df_results = pd.read_csv(results_file)
            st.download_button(
                "Full results CSV",
                data=df_results.to_csv(index=False),
                file_name=f"docking_results_{results_job_id}.csv",
                mime="text/csv",
                use_container_width=True,
            )
            df_best = df_results.loc[
                df_results.groupby(["ligand", "receptor"])["affinity (kcal/mol)"].idxmin()
            ]
            st.download_button(
                "Best-poses CSV",
                data=df_best.to_csv(index=False),
                file_name=f"docking_best_poses_{results_job_id}.csv",
                mime="text/csv",
                use_container_width=True,
            )
            # B11.21: ZIP of the whole run — per-pose SDFs + both CSVs.
            output_dir = (
                info.get("docking_output_dir")
                or os.path.join(str(Config.RESULTS_DIR), f"dock_{results_job_id}")
            )
            if output_dir and os.path.isdir(str(output_dir)):
                zip_bytes, omitted = _results_zip_cached(str(output_dir), results_job_id)
                st.download_button(
                    "Download all results (ZIP)",
                    data=zip_bytes,
                    file_name=f"docking_results_{results_job_id}.zip",
                    mime="application/zip",
                    use_container_width=True,
                )
                if omitted:
                    cap_mb = Config.MAX_DOWNLOAD_ZIP_SIZE // (1024 * 1024)
                    st.caption(
                        f"{len(omitted)} large pose file(s) omitted to keep "
                        f"the archive under ~{cap_mb} MB."
                    )
        except Exception as e:  # noqa: BLE001
            st.caption(f"Could not prepare downloads: {e}")


def render(session, is_editor: bool) -> None:
    task = _live_task_or_none()
    if task is not None:
        _render_running(task, session, is_editor)
        return

    # B11.10: Re-dock / New-docking-run button sets this flag; honor it
    # before the DB-driven results dispatch.
    if st.session_state.get("docking_force_settings"):
        _render_settings(session, is_editor)
        return

    # B11.20: a run-selector dropdown or a Jobs-list click can pin a
    # specific (possibly non-latest) docking job via docking_view_job_id;
    # fall back to the latest run when unset or stale.
    view_id = st.session_state.get("docking_view_job_id")
    job = job_by_legacy_id(session.id, view_id) if view_id else None
    if job is None:
        job = latest_job_of_kind(session.id, _RESULTS_KINDS)
    if job is not None:
        _render_results(job, is_editor, session)
        return

    _render_settings(session, is_editor)
