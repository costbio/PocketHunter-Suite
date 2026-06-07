"""v2 analysis page — persistent Mol* viewer + stage-aware right pane.

Phase B commit B6. The viewer column and the jobs panel are wrapped in
``@st.fragment(run_every="3s")`` blocks so completed analyses surface
in the viewer (as pocket / cluster annotations) and in the job list
within a few seconds, without forcing a full page rerun. Panel-level
``AsyncResult`` polling stays in place — it drives the in-panel
progress bar; B7 retires it alongside the legacy pages.

Layout:

    ┌───────────────────────────────────────────────────────────┐
    │ POCKETHUNTER/SUITE [v.2.0]                                │
    │ ● POCKETS ► ● CLUSTER ► ○ DOCK   (dynamic stage strip)    │
    ├───────────────────────────────────────────────────────────┤
    │ [Run all stages]   ( Pockets · Cluster · Dock )           │
    ├───────────────────────────┬───────────────────────────────┤
    │                           │                               │
    │   Mol* viewer (fragment)  │   Active stage panel          │
    │   re-derives annotations  │                               │
    │   from DB every 3 s       │                               │
    │                           │                               │
    └───────────────────────────┴───────────────────────────────┘
    │ ▾ Jobs (fragment)                                         │
    └───────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

import streamlit as st


# Base dir for the ``static/`` route. ``analysis_app.py`` lives at the
# repo root, so ``Path(__file__).parent`` is the canonical anchor.
_BASE_DIR = Path(__file__).parent


# Stage indicator order — left-to-right in the header strip.
_STAGE_INDICATORS = (
    ("find_pockets", "POCKETS"),
    ("cluster", "CLUSTER"),
    ("docking", "DOCK"),
)


# Body-column ratio (viewer : panel). 50/50 in B11; tweak here if the
# viewer needs more or less room.
_BODY_COLUMN_RATIO = (1, 1)


# Map each stage segment to the kind that must be completed for it to
# unlock. Used for the progressive-disclosure ``st.segmented_control``.
# "Pockets" is always available; "Cluster" needs a completed find_pockets
# (or pipeline) job; "Dock" needs a completed cluster (or pipeline) job.
_STAGE_UNLOCK_REQUIREMENT = {
    "Pockets": None,
    "Cluster": "find_pockets",
    "Dock": "cluster",
}


# Job ``kind`` values that can produce a ``viewer_file_path``.
_VIEWER_PRODUCING_KINDS = ("find_pockets",)


def _compute_completed_kinds(jobs: list[dict]) -> set[str]:
    """Return the set of analysis kinds with at least one completed job."""
    completed: set[str] = set()
    for row in jobs or []:
        if row.get("status") not in ("completed", "SUCCESS", "success"):
            continue
        kind = row.get("kind")
        if kind:
            completed.add(kind)
    return completed


def _render_header(resolved) -> None:
    """Delegates to ``landing.render_masthead`` — single source of truth.

    Kept as a thin wrapper so the analysis-page render flow doesn't grow
    a direct ``import landing`` at module top (avoids a circular import:
    landing.py already imports from session_routes which analysis_app
    also touches). Lazy import inside the function sidesteps that risk.
    """
    from landing import render_masthead_fragment
    render_masthead_fragment(resolved=resolved)


def _pick_latest_viewer(jobs: Iterable[dict]) -> Optional[dict]:
    """Return the newest job whose ``result_info`` references a viewer file."""
    candidates = []
    for row in jobs or []:
        if row.get("kind") not in _VIEWER_PRODUCING_KINDS:
            continue
        info = row.get("result_info") or {}
        if not isinstance(info, dict):
            continue
        if not info.get("viewer_file_path"):
            continue
        candidates.append(row)
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.get("last_updated") or "")


def _pick_latest_pockets_job(jobs: Iterable[dict]) -> Optional[dict]:
    """Return the newest completed find_pockets / pipeline job, viewer file or not.

    Used to distinguish "no analysis yet" from "analysis ran but viewer
    file generation was skipped or failed" — the latter cases carry
    ``viewer_file_warning`` / ``viewer_file_error`` in ``result_info``.
    """
    candidates = []
    for row in jobs or []:
        if row.get("kind") not in _VIEWER_PRODUCING_KINDS:
            continue
        if row.get("status") not in ("completed", "SUCCESS", "success"):
            continue
        candidates.append(row)
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.get("last_updated") or "")


@st.fragment
def _viewer_fragment(resolved) -> None:
    """Fragment that owns the Mol* viewer column.

    B11.7: dropped ``run_every="3s"`` — direct measurement showed the
    auto-poll re-mounted Mol* at ~1.8 Hz (5× the declared rate, likely
    from a self-amplifying cycle between the component's
    ``setStateValue("annotations_applied", ...)`` and Streamlit's
    rerun trigger). The constant churn flooded the WebSocket, making
    every other user action wait behind queued mount work.

    With the auto-poll gone, the fragment only re-runs when:

    - Its own widget (the frame slider) fires.
    - The surrounding page reruns from any cause (button click, panel
      `st.rerun()`, the panel-side `render_running_progress` loop
      while a task is in PROGRESS).

    Live-task updates remain covered by ``render_running_progress``
    in ``panels._shared``, which fires page-level reruns every 3 s
    while a task is running — same cadence as the old fragment poll.
    """
    from config import Config
    from failure_view import load_status_for_session
    from panels._shared import viewer_target_for_filename
    from viewer_pipeline import VIEWER_FILE_FORMAT, link_viewer_for_session

    session = resolved.session
    jobs = load_status_for_session(session.id)
    latest = _pick_latest_viewer(jobs)

    if latest is None:
        # No usable viewer file — distinguish "no analysis yet" from
        # "analysis ran but viewer file was skipped/errored".
        completed = _pick_latest_pockets_job(jobs)
        if completed is None:
            st.info(
                "No trajectory available yet. Run **Find Pockets** to "
                "populate the viewer."
            )
            return
        info = completed.get("result_info") or {}
        warning = info.get("viewer_file_warning") if isinstance(info, dict) else None
        error = info.get("viewer_file_error") if isinstance(info, dict) else None
        frames = info.get("frames_extracted") if isinstance(info, dict) else None
        if warning:
            st.warning(
                f"**Viewer file skipped.** {warning}\n\n"
                "Re-run Find Pockets with a higher stride (fewer frames "
                "extracted) to bring the estimated size under the limit. "
                f"This run extracted {frames or '?'} frames."
            )
        elif error:
            st.error(f"**Viewer file generation failed.** {error}")
        else:
            st.warning(
                "Find Pockets completed but no viewer file is on disk. The "
                "session results may have been pruned, or the viewer "
                "pipeline ran on an older codebase that didn't emit one."
            )
        return

    info = latest.get("result_info") or {}
    viewer_path = Path(info["viewer_file_path"])
    if not viewer_path.exists():
        st.warning(
            "Viewer trajectory no longer available on disk. The session may "
            "have been pruned. Re-run **Find Pockets** to regenerate it."
        )
        return

    try:
        url = link_viewer_for_session(
            session.short_code,
            viewer_path,
            _BASE_DIR,
        )
    except OSError as exc:
        st.error(
            "Failed to make the viewer trajectory available to the browser. "
            f"({exc})"
        )
        return

    fmt = info.get("viewer_file_format") or VIEWER_FILE_FORMAT
    from components.molstar_annotations import derive_session_annotations
    from components.molstar_viewer import molstar_viewer

    # Streamlit's bidi-component key validator rejects ``__`` in the
    # base. The central :func:`db.sessions.safe_bidi_short` handles
    # both legacy short_codes containing ``__`` AND short_codes
    # starting/ending with ``_`` (which would otherwise produce ``__``
    # once concatenated as ``f"viewer_{...}"`` below).
    from db.sessions import safe_bidi_short
    safe_short = safe_bidi_short(session.short_code)

    # DB-derived sections (pockets, clusters) overlaid by panel-set UI
    # state (ligand_pose, focus). UI state wins — the docking panel's
    # last pose selection survives the fragment's poll.
    base = derive_session_annotations(session.id, Path(Config.RESULTS_DIR))
    ann_key = f"viewer_annotations_{safe_short}"
    ui_overrides = st.session_state.get(ann_key, {})
    annotations = {**base, **ui_overrides}

    click_key = f"residue_clicked_{safe_short}"

    def _on_residue_clicked() -> None:
        result = st.session_state.get(f"viewer_{safe_short}")
        st.session_state[click_key] = (
            getattr(result, "residue_clicked", None) if result else None
        )

    # Two-key frame state: ``target_key`` (the source of truth for what
    # frame is displayed) and ``slider_key`` (the slider widget's own
    # session_state). Decoupled because Streamlit silently rejects
    # writes to a widget's key from outside the widget *after* the
    # widget has rendered — so panels that want to drive the slider
    # have to push into ``target_key`` instead. The slider's
    # ``on_change`` callback keeps the two in sync when the user drags.
    #
    # B12 (viewer stride): ``target_key`` now carries the *logical
    # filename* (e.g. ``trajectory_..._fit_84.pdb``) rather than a dense
    # model index. The fragment resolves it to a :class:`ViewerFrameTarget`
    # via the manifest; if the frame is in the strided set we stay in
    # overview mode (slider + setCurrentModel), else focused mode
    # (single-frame URL + slider hidden).
    n_extracted = int(info.get("n_extracted_frames") or info.get("frames_extracted") or 1)
    n_viewer_models = int(info.get("n_viewer_models") or 0)
    if n_viewer_models <= 0:
        # Legacy job (built before the stride feature) — manifest is
        # missing, so the dense map *is* the viewer's model list.
        n_viewer_models = n_extracted

    target_key = f"viewer_frame_target_{safe_short}"
    slider_key = f"viewer_frame_slider_{safe_short}"
    pockets_legacy_id = (latest or {}).get("legacy_id") or ""

    def _resolve_target_filename(value):
        """Accept the new (filename) and legacy (dense int) shapes."""
        if value is None:
            return None
        if isinstance(value, str):
            return value
        # Legacy callers wrote an int — translate it to a logical filename
        # via the dense map so the fragment's branch logic stays uniform.
        if isinstance(value, int):
            from panels._shared import _frame_index_map  # local: cheap
            for fname, idx in _frame_index_map(pockets_legacy_id).items():
                if idx == value:
                    return fname
        return None

    # B11.18: on the Dock stage, the bottom slider switches semantics —
    # it walks the docking receptors (each at its own trajectory frame)
    # instead of scrubbing raw frames. The docking panel publishes the
    # ordered receptor list into ``docking_receptors_<short>``.
    active_stage = st.session_state.get("active_stage") or "Pockets"
    receptors = st.session_state.get(f"docking_receptors_{safe_short}", [])
    dock_mode = active_stage == "Dock" and bool(receptors)

    mode = "overview"        # overview | focused
    current_model = 1        # 1-based index into viewer.pdb (overview)
    focused_url = None       # set in focused mode → single-frame PDB URL
    focused_logical = None   # 1-based logical index for the "Frame N" label
    # Resolved on-disk filename of whichever frame is currently rendered.
    # Used by the "↓ Download view" button below to round-trip the user
    # back to a per-frame PDB.
    current_logical_filename = None
    current_logical_index = None

    if dock_mode:
        recv_idx_key = f"docking_active_receptor_idx_{safe_short}"
        recv_idx = max(0, min(int(st.session_state.get(recv_idx_key, 0)), len(receptors) - 1))
        rec = receptors[recv_idx]
        rec_filename = rec.get("frame_filename")
        rec_target = (
            viewer_target_for_filename(pockets_legacy_id, rec_filename)
            if rec_filename else None
        )
        if rec_target is None:
            # Legacy receptor row (frame_model_index only) — stay in
            # overview at whatever index the panel published.
            fmi = rec.get("frame_model_index")
            current_model = int(fmi) if fmi else 1
            current_logical_filename = rec_filename  # may be None
        elif rec_target.viewer_model is not None:
            current_model = int(rec_target.viewer_model)
            current_logical_filename = rec_target.filename
            current_logical_index = rec_target.logical_index
        else:
            mode = "focused"
            focused_logical = rec_target.logical_index
            focused_url = (
                f"/app/static/{safe_short}/frames/{rec_target.filename}"
            )
            current_logical_filename = rec_target.filename
            current_logical_index = rec_target.logical_index
    else:
        target_value = _resolve_target_filename(st.session_state.get(target_key))
        target = (
            viewer_target_for_filename(pockets_legacy_id, target_value)
            if target_value else None
        )
        if target is None:
            # No selection (or legacy int couldn't be resolved) — overview at frame 1.
            if n_viewer_models > 1:
                st.session_state.setdefault(target_key, None)
            current_model = 1
            # Resolve model 1 → its logical filename via the manifest's
            # inverse so the download button still works at idle.
            from panels._shared import _viewer_manifest, _manifest_mtime_ns
            _m = _viewer_manifest(pockets_legacy_id, _manifest_mtime_ns(pockets_legacy_id))
            if _m:
                _inv = {
                    int(idx): fn
                    for fn, idx in (_m.get("logical_to_viewer") or {}).items()
                }
                current_logical_filename = _inv.get(1)
        elif target.viewer_model is not None:
            current_model = int(target.viewer_model)
            if st.session_state.get(slider_key) != current_model:
                st.session_state[slider_key] = current_model
            current_logical_filename = target.filename
            current_logical_index = target.logical_index
        else:
            mode = "focused"
            focused_logical = target.logical_index
            focused_url = (
                f"/app/static/{safe_short}/frames/{target.filename}"
            )
            current_logical_filename = target.filename
            current_logical_index = target.logical_index

    # B11.1: bumped from 460 → 540. With the lonely segmented_control
    # hidden on fresh sessions (and tightened metric strips elsewhere),
    # the viewer column has the room. Adjust here if a future contributor
    # wants a different default.
    viewer_h = 540

    # Surface JS-side load failures (Mol* threw inside loadStructureFromUrl).
    # The component records the error in its state via load_error; without
    # this callback, the failure is swallowed and the user sees an empty
    # pane with no idea why.
    load_error_key = f"viewer_load_error_{safe_short}"

    def _on_load_error() -> None:
        result = st.session_state.get(f"viewer_{safe_short}")
        err = getattr(result, "load_error", None) if result else None
        st.session_state[load_error_key] = err

    # Pick the structure URL based on mode. Focused mode swaps to the
    # single-frame PDB under ``static/<short>/frames/`` — Mol*'s
    # ``loadStructure`` replaces the whole trajectory on URL change.
    if mode == "focused" and focused_url:
        active_structure_url = focused_url
        active_current_model = 1
    else:
        active_structure_url = url
        active_current_model = current_model

    molstar_viewer(
        structure_url=active_structure_url,
        structure_format=fmt,
        annotations=annotations,
        current_model=active_current_model,
        key=f"viewer_{safe_short}",
        height=viewer_h,
        on_residue_clicked_change=_on_residue_clicked,
        on_load_error_change=_on_load_error,
    )

    load_err = st.session_state.get(load_error_key)
    if load_err:
        st.error(
            f"**Mol* couldn't load this trajectory.** {load_err}\n\n"
            f"Common causes: too many frames for the browser to parse "
            f"in memory; the structure has corrupt atom records; or the "
            f"WebGL context was lost. Re-run Find Pockets with a larger "
            f"stride (fewer frames) or try a different browser."
        )

    # Inline "download what's currently shown" button. Renders the
    # currently-rendered single frame as PDB; bundles the active ligand
    # pose (when present) into a ZIP alongside it. Hidden only when we
    # genuinely have no on-disk frame to point at (e.g. an aborted job).
    if current_logical_filename and pockets_legacy_id:
        from downloads import current_view_artifact

        frame_pdb_path = (
            Path(Config.RESULTS_DIR)
            / pockets_legacy_id
            / "pdbs"
            / current_logical_filename
        )
        if frame_pdb_path.exists():
            pose = annotations.get("ligand_pose") if isinstance(annotations, dict) else None
            pose_sdf = pose.get("sdf") if isinstance(pose, dict) else None
            pose_label = pose.get("label") if isinstance(pose, dict) else None
            label_n = current_logical_index or focused_logical or current_model
            frame_label = f"F{label_n}"
            try:
                data, fname, mime = current_view_artifact(
                    structure_path=frame_pdb_path,
                    ligand_pose_sdf=pose_sdf,
                    session_short=safe_short,
                    frame_label=frame_label,
                    ligand_label=pose_label,
                )
                st.download_button(
                    label=(
                        "↓ Download view (complex)"
                        if pose_sdf
                        else "↓ Download view (PDB)"
                    ),
                    data=data,
                    file_name=fname,
                    mime=mime,
                    key=f"viewer_download_{safe_short}",
                    help=(
                        "Download the currently rendered frame. "
                        + (
                            "Bundles the active ligand pose alongside it as a ZIP."
                            if pose_sdf
                            else "Single-frame PDB suitable for PyMOL / ChimeraX."
                        )
                    ),
                )
            except OSError:
                pass  # disk read failed; quietly omit the button

    if dock_mode:
        # B11.18: receptor slider — walks the selected ligand's pose
        # across docking receptors. Each receptor sits at its own
        # trajectory frame; selecting one drives both the viewer's
        # ``current_model`` (above) and the docking panel's active
        # column.
        recv_idx_key = f"docking_active_receptor_idx_{safe_short}"
        recv_slider_key = f"docking_receptor_slider_{safe_short}"
        labels = [r["label"] for r in receptors]
        recv_idx = max(0, min(int(st.session_state.get(recv_idx_key, 0)), len(labels) - 1))

        # Sync the widget's own state to the active index *before* it
        # renders — handles the docking panel snapping the receptor on
        # a ligand-row click (it writes recv_idx_key, not the widget key).
        desired = labels[recv_idx]
        if st.session_state.get(recv_slider_key) != desired:
            st.session_state[recv_slider_key] = desired

        def _on_receptor_change():
            # B11.19: NO st.rerun() here — it's a no-op inside a
            # callback. The callback only updates the active index;
            # the app-scope escalation happens in the fragment body
            # below.
            chosen = st.session_state[recv_slider_key]
            try:
                new_idx = labels.index(chosen)
            except ValueError:
                new_idx = 0
            st.session_state[recv_idx_key] = new_idx

        st.select_slider(
            "Receptor",
            options=labels,
            key=recv_slider_key,
            on_change=_on_receptor_change,
            help="Slide to view the selected ligand's pose at each receptor.",
        )

        # B11.19: the slider's natural rerun is fragment-scoped — it
        # re-mounts THIS viewer but leaves the docking panel stale.
        # Escalate to an app-scope rerun when the receptor index
        # actually changed so the panel re-pushes the pose for the new
        # receptor and moves its column highlight.
        propagated_key = f"docking_recv_propagated_{safe_short}"
        cur_idx = int(st.session_state.get(recv_idx_key, 0))
        if propagated_key not in st.session_state:
            st.session_state[propagated_key] = cur_idx
        elif st.session_state[propagated_key] != cur_idx:
            st.session_state[propagated_key] = cur_idx
            st.rerun(scope="app")
    elif mode == "focused":
        # Single-frame mode: slider is meaningless (we've replaced the
        # trajectory with a one-model PDB), so swap it for a return button.
        cap_label = (
            f"Frame {focused_logical} of {n_extracted}"
            if focused_logical else "Single frame"
        )
        cols = st.columns([4, 1])
        with cols[0]:
            st.caption(
                f"Showing **{cap_label}** — single-frame mode "
                f"(this frame isn't in the {n_viewer_models}-model overview)."
            )
        with cols[1]:
            if st.button(
                "↩ Overview",
                key=f"viewer_focused_exit_{safe_short}",
                use_container_width=True,
                help="Return to the strided trajectory scrub view.",
            ):
                st.session_state.pop(target_key, None)
                # Clear last-select sentinels so the next pocket click re-fires.
                for k in list(st.session_state.keys()):
                    if isinstance(k, str) and (
                        k.startswith(f"fp_last_pocket_select_{safe_short}")
                        or k.startswith(f"cluster_last_select_{safe_short}")
                    ):
                        st.session_state.pop(k, None)
                st.rerun()
    elif n_viewer_models > 1:
        def _sync_target():
            # User-driven drag → mirror the slider's value back into the
            # target key so the next render picks it up. The slider walks
            # the strided viewer models 1..n_viewer_models; we resolve
            # the corresponding logical filename via the manifest's
            # inverse map so the target remains filename-shaped.
            from panels._shared import _viewer_manifest, _manifest_mtime_ns

            new_idx = int(st.session_state[slider_key])
            manifest = _viewer_manifest(
                pockets_legacy_id, _manifest_mtime_ns(pockets_legacy_id)
            )
            inverse = {}
            if manifest:
                for fname, idx in (manifest.get("logical_to_viewer") or {}).items():
                    inverse[int(idx)] = fname
            st.session_state[target_key] = inverse.get(new_idx)

        st.slider(
            f"Frame {current_model} / {n_viewer_models}",
            min_value=1,
            max_value=n_viewer_models,
            step=1,
            key=slider_key,
            on_change=_sync_target,
            label_visibility="visible",
            help=(
                "Drag to scrub through the strided trajectory. Click a "
                "pocket / receptor row to jump to its exact frame "
                "(loads on demand if outside this strided set)."
            ),
        )


@st.fragment
def _jobs_panel_fragment(resolved) -> None:
    """Fragment that renders the session's job list.

    B11.8: dropped ``run_every="3s"`` — idle polling adds background
    WebSocket traffic that competes with user clicks under WSL2
    mirrored networking. In-flight task updates already drive page
    reruns at the same cadence via the panel-side
    ``render_running_progress`` loop (``panels._shared:168-210``),
    which naturally re-runs this fragment.
    """
    from panels.jobs_panel import render as render_jobs_panel

    render_jobs_panel(resolved.session)


@st.fragment(run_every="3s")
def _live_log_fragment(resolved) -> None:
    """Fragment that tails the active task's stdout.

    B11.20: restored ``run_every="3s"``. B11.8 had dropped it on the
    assumption that the panel-side ``render_running_progress`` loop
    fires page reruns that re-run this fragment — but B11.18 moved the
    docking running view into its own isolated fragment, so nothing
    re-runs this one while a docking job streams. The body still
    short-circuits via ``any_task_running()`` when idle, so the
    idle cost is one cheap DB query per tick.

    Renders nothing when no task is in flight; the surrounding
    container shrinks out of the layout entirely. When a task is
    running, mounts an expanded ``Live log`` expander with the last
    ~50 lines of the most-recent stage's stdout and a header line
    showing kind + legacy_id + stage + elapsed seconds.
    """
    from pathlib import Path

    from config import Config
    from live_log import (
        any_task_running,
        find_active_stage_log,
        list_active_running_jobs,
        tail_log,
    )

    session = resolved.session
    if not any_task_running(session.id):
        return

    results_dir = Path(Config.RESULTS_DIR)
    running = list_active_running_jobs(session.id)
    header = "Live log"
    if running:
        top = running[0]
        kind = top.get("kind", "?")
        legacy = top.get("legacy_id", "?")
        step = (top.get("step") or "").strip()
        header_parts = [f"{kind} · `{legacy}`"]
        if step:
            header_parts.append(step)
        header = "Live log — " + " · ".join(header_parts)

    with st.expander(header, expanded=True):
        log_path = find_active_stage_log(session.id, results_dir)
        if log_path is None:
            st.caption(
                "The worker hasn't started writing logs yet — first lines "
                "should appear within a couple seconds."
            )
            return
        content = tail_log(log_path, max_lines=50)
        if not content:
            st.caption(f"`{log_path.name}` is empty so far.")
            return
        st.caption(f"Tailing `{log_path.name}` — last 50 lines (refreshes every ~3 s).")
        st.code(content, language="text")


def _render_panel_column(resolved, active_stage: str) -> None:
    """Dispatch to the per-stage panel module."""
    session = resolved.session
    is_editor = bool(resolved.is_editor)

    # B11.20: the docking "force settings" intent is stale once the user
    # is on another stage — bound the sticky flag so navigating back to
    # Dock via the segmented control shows results again.
    if active_stage != "Dock":
        st.session_state.pop("docking_force_settings", None)

    # B11.20: a Jobs-list click / docking run-selector pins a specific
    # job via ``<stage>_view_job_id``; bound it to the owning stage so
    # leaving and returning resets the panel to the latest run.
    for stage, key in (
        ("Pockets", "fp_view_job_id"),
        ("Cluster", "cluster_view_job_id"),
        ("Dock", "docking_view_job_id"),
    ):
        if stage != active_stage:
            st.session_state.pop(key, None)

    if active_stage == "Pockets":
        from panels.find_pockets import render
    elif active_stage == "Cluster":
        from panels.cluster import render
    elif active_stage == "Dock":
        from panels.docking import render
    else:
        st.error(f"Unknown stage: {active_stage}")
        return

    render(session, is_editor)


def render_analysis_app(resolved) -> None:
    """Render the v2 analysis page."""
    from failure_view import load_status_for_session

    # B11.1: drain the "pending active stage" intermediate key set by
    # panels' "Next: <Stage> →" buttons BEFORE we instantiate the
    # ``st.segmented_control`` below. Streamlit's widget-state ownership
    # rule blocks writes to ``active_stage`` after that widget renders,
    # so panels write to ``pending_active_stage`` and we drain it here
    # — at this point the widget hasn't rendered yet this pass.
    pending = st.session_state.pop("pending_active_stage", None)
    if pending is not None:
        st.session_state["active_stage"] = pending

    # Snapshot session jobs once per full rerun so the header stage
    # indicator stays consistent with the page-level state. The viewer
    # + jobs fragments re-query the DB on their own polling schedule.
    session_jobs = load_status_for_session(resolved.session.id) or []
    completed_kinds = _compute_completed_kinds(session_jobs)

    _render_header(resolved)

    # B11.22: the "New session" affordance now lives inside the header
    # row (see _render_header) — the standalone button row is gone.

    # Progressive disclosure: build the segmented_control's options list
    # from ``completed_kinds`` so locked stages don't appear. Each entry
    # in ``_STAGE_UNLOCK_REQUIREMENT`` either has a None requirement
    # (always shown) or a kind that must be in ``completed_kinds``.
    unlocked: list[str] = []
    for stage, requirement in _STAGE_UNLOCK_REQUIREMENT.items():
        if requirement is None or requirement in completed_kinds:
            unlocked.append(stage)

    # B11.20: Dock also unlocks once pockets are staged for docking (the
    # bucket can be filled straight from the Pockets panel, before
    # clustering) or a docking job already exists — otherwise the
    # stage-unlock guard below swallows the "Go to Docking →" jump.
    if "Dock" not in unlocked:
        from panels import _docking_bucket as docking_bucket
        if docking_bucket.count() > 0 or "docking" in completed_kinds:
            unlocked.append("Dock")

    # Force-reset ``active_stage`` if the user is sitting on a stage that
    # got locked (e.g., results were pruned after a refresh, or session
    # state was carried over from a previous session via NEW SESSION
    # click). Keeps the segmented_control's value in sync with its
    # current options.
    #
    # NB: NO st.rerun() here. We update session_state BEFORE the
    # segmented_control widget instantiates a few lines down; per
    # Streamlit's widget-state rules, the widget reads the new value at
    # creation time. Re-running would cause an extra render iteration —
    # user-visible as a flicker on NEW SESSION navigation.
    current = st.session_state.get("active_stage") or "Pockets"
    if current not in unlocked:
        st.session_state["active_stage"] = unlocked[-1]

    # B11.1: only render the segmented_control when there's a real
    # choice — a lone "Pockets" button above an empty column looks
    # aimless and pushes the viewer down. Hidden on fresh sessions;
    # reappears once find_pockets completes.
    if len(unlocked) > 1:
        st.segmented_control(
            "Stage",
            options=unlocked,
            key="active_stage",
            default="Pockets",
            label_visibility="collapsed",
        )

    active_stage = st.session_state.get("active_stage") or "Pockets"

    viewer_col, panel_col = st.columns(_BODY_COLUMN_RATIO, gap="medium")
    with viewer_col:
        _viewer_fragment(resolved)
    with panel_col:
        _render_panel_column(resolved, active_stage)

    # Live log sits between the columns and the Jobs panel. The fragment
    # renders nothing when no task is in flight, so the layout shrinks
    # cleanly when the session is idle.
    _live_log_fragment(resolved)

    # B11.2: persistent docking-selection strip. Visible only when the
    # cross-stage bucket has at least one pocket. Clear / Go-to-Docking
    # shortcuts make the selection actionable from any panel.
    # B11.20: hidden on the Dock stage — once you're on Dock (setup,
    # running, or results) the nudge-to-docking strip is redundant.
    if active_stage != "Dock":
        _render_docking_selection_strip(resolved)

    # Jobs panel sits below, full-width.
    with st.expander("Jobs", expanded=False):
        _jobs_panel_fragment(resolved)


def _render_docking_selection_strip(resolved) -> None:
    """Bottom strip showing the docking-selection count + actions.

    Renders only when ``panels._docking_bucket`` has at least one
    pocket — zero-cost when nothing's selected.
    """
    from panels import _docking_bucket as docking_bucket

    n = docking_bucket.count()
    if n == 0:
        return

    with st.container(border=True):
        cols = st.columns([6, 1, 2])
        with cols[0]:
            st.markdown(
                f"**{n} pocket{'s' if n != 1 else ''} selected for docking**"
            )
        with cols[1]:
            if st.button(
                "Clear",
                key="dock_strip_clear",
                use_container_width=True,
                disabled=not resolved.is_editor,
            ):
                docking_bucket.clear()
                st.rerun()
        with cols[2]:
            if st.button(
                "Go to Docking →",
                key="dock_strip_go",
                use_container_width=True,
                type="primary",
            ):
                # B11.1 pattern — write to the pending key so it's drained
                # at the top of the next render before the segmented_control
                # instantiates.
                st.session_state["pending_active_stage"] = "Dock"
                # B11.20: land on the docking setup form (with the staged
                # pockets), not the old results view, when a docking job
                # already exists for the session.
                st.session_state["docking_force_settings"] = True
                st.rerun()
