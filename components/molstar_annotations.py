"""Python-side builders that turn analysis result CSVs into annotation dicts
consumed by ``molstar_viewer(annotations=...)``.

The annotation schema (mirrored by the JS in ``molstar_viewer.py``)::

    {
        "pockets": [
            {"residues": ["A_125", "A_147"], "color": "#FF5733", "label": "Pocket 1"},
            ...
        ],
        "clusters": [
            {"cluster_id": 0, "residues": ["A_150", ...], "color": "#33FF57"},
            ...
        ],
        "ligand_pose": {"sdf": "<v2000 sdf text>", "color": "#FFA500", "label": "ligand_x"} | None,
        "focus": {"type": "pockets" | "clusters" | "ligand" | "all", "target": int | None} | None,
    }

Each panel calls one of these builders + :func:`merge_annotations` to push
its section into the session-scoped dict that ``analysis_app.py`` forwards
to the component.

These helpers are pure: no Streamlit imports, no I/O beyond reading
``pose["output_sdf"]`` via :func:`docking_visualization.extract_sdf_model`.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd


# Hard-coded palette so this module doesn't pull matplotlib in. The cycle
# mirrors matplotlib's tab10 — distinct hues that stay legible against the
# Mol* black background.
_PALETTE_HEX = (
    "#1f77b4",  # blue
    "#ff7f0e",  # orange
    "#2ca02c",  # green
    "#d62728",  # red
    "#9467bd",  # purple
    "#8c564b",  # brown
    "#e377c2",  # pink
    "#7f7f7f",  # gray
    "#bcbd22",  # olive
    "#17becf",  # cyan
)

# Mol* surfaces with fewer residues than this collapse into single-atom
# blobs or fail to mesh entirely. Skip them at the source.
_MIN_RESIDUES_FOR_SURFACE = 3

# Fixed orange used everywhere a docking ligand renders.
_LIGAND_COLOR_HEX = "#ffa500"


def _palette(n: int) -> list[str]:
    """Return ``n`` distinct hex colors, cycling :data:`_PALETTE_HEX`."""
    if n <= 0:
        return []
    out = []
    for i in range(n):
        out.append(_PALETTE_HEX[i % len(_PALETTE_HEX)])
    return out


def _parse_residues(value: Any) -> list[str]:
    """Parse a ``residues`` cell into a list of ``<chain>_<seqid>`` strings.

    p2rank writes the column as a space-separated string (``"A_125 A_147"``).
    Empty / NaN cells return ``[]``.
    """
    if value is None:
        return []
    if isinstance(value, float):
        # pandas NaN
        if pd.isna(value):
            return []
        value = str(value)
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value if x]
    return [tok for tok in str(value).split() if tok]


def pocket_annotations_from_df(
    df_pockets: pd.DataFrame,
    top_n: int = 5,
) -> list[dict]:
    """Build pocket-overlay annotations from a ``pockets.csv``-shaped DataFrame.

    Picks the ``top_n`` highest-probability rows, drops any whose residue
    list is below the surface-meshing threshold, and assigns palette colors.

    Args:
        df_pockets: DataFrame with at least ``probability`` and ``residues``
            columns. Other columns (``File name``, ``pocket_index``) are
            optional and surfaced via the ``label`` field when present.
        top_n: Maximum number of pockets to return.

    Returns:
        List of dicts of shape
        ``{"residues": [...], "color": "#...", "label": "Pocket N"}``.
    """
    if df_pockets is None or len(df_pockets) == 0:
        return []
    if "probability" not in df_pockets.columns or "residues" not in df_pockets.columns:
        return []

    ordered = df_pockets.sort_values("probability", ascending=False)
    out: list[dict] = []
    for idx, row in ordered.iterrows():
        residues = _parse_residues(row.get("residues"))
        if len(residues) < _MIN_RESIDUES_FOR_SURFACE:
            continue
        label_parts = []
        if "pocket_index" in row and pd.notna(row.get("pocket_index")):
            label_parts.append(f"Pocket {int(row['pocket_index'])}")
        elif "File name" in row and pd.notna(row.get("File name")):
            label_parts.append(str(row["File name"]))
        else:
            label_parts.append(f"Pocket {len(out) + 1}")
        prob = row.get("probability")
        if prob is not None and not pd.isna(prob):
            label_parts.append(f"p={float(prob):.2f}")
        out.append(
            {
                "residues": residues,
                "label": " · ".join(label_parts),
            }
        )
        if len(out) >= top_n:
            break

    for color, entry in zip(_palette(len(out)), out):
        entry["color"] = color
    return out


def cluster_annotations_from_df(
    df_clustered: Optional[pd.DataFrame],
    df_reps: Optional[pd.DataFrame],
) -> list[dict]:
    """Build cluster-overlay annotations from the cluster pipeline outputs.

    For each cluster id present in ``df_reps``, the residue set is the
    union of every member pocket's residues in ``df_clustered`` (if
    available) — falling back to the representative's own residue list
    when ``df_clustered`` is missing.

    Args:
        df_clustered: ``pockets_clustered.csv``-shaped DataFrame (one row
            per pocket; ``cluster`` column). May be ``None``.
        df_reps: ``cluster_representatives.csv``-shaped DataFrame. Required.

    Returns:
        List of cluster annotation dicts. Empty list when ``df_reps`` is
        missing the expected columns.
    """
    if df_reps is None or "cluster" not in df_reps.columns:
        return []

    cluster_ids = sorted(int(c) for c in df_reps["cluster"].dropna().unique())
    palette = _palette(len(cluster_ids))

    out: list[dict] = []
    for color, cid in zip(palette, cluster_ids):
        residues: set[str] = set()
        if df_clustered is not None and "cluster" in df_clustered.columns:
            for _, row in df_clustered[df_clustered["cluster"] == cid].iterrows():
                residues.update(_parse_residues(row.get("residues")))
        if not residues:
            for _, row in df_reps[df_reps["cluster"] == cid].iterrows():
                residues.update(_parse_residues(row.get("residues")))
        if not residues:
            continue
        out.append(
            {
                "cluster_id": cid,
                "residues": sorted(residues),
                "color": color,
            }
        )
    return out


def ligand_pose_annotation_from_pose(pose: Optional[dict]) -> Optional[dict]:
    """Build a single ``ligand_pose`` annotation from a docking-result row.

    Args:
        pose: A row dict from the docking results DataFrame. Must include
            ``output_sdf`` (path on disk) and ``mode`` (1-based pose index
            within the multi-model SDF). Other keys (``ligand``,
            ``affinity (kcal/mol)``) optionally feed the label.

    Returns:
        Annotation dict ``{"sdf": ..., "color": ..., "label": ...}`` or
        ``None`` if the SDF can't be extracted (file missing, mode index
        out of range, helper raises). Caller skips the merge on ``None``.
    """
    if not pose or not isinstance(pose, dict):
        return None
    sdf_path = pose.get("output_sdf")
    mode = pose.get("mode")
    if not sdf_path or mode is None:
        return None

    try:
        from docking_visualization import extract_sdf_model

        sdf_text = extract_sdf_model(str(sdf_path), int(mode))
    except Exception:
        return None
    if not sdf_text:
        return None

    label_parts = []
    if pose.get("ligand"):
        label_parts.append(str(pose["ligand"]))
    if "affinity (kcal/mol)" in pose:
        try:
            label_parts.append(f"{float(pose['affinity (kcal/mol)']):.2f} kcal/mol")
        except (TypeError, ValueError):
            pass
    return {
        "sdf": sdf_text,
        "color": _LIGAND_COLOR_HEX,
        "label": " · ".join(label_parts) or "ligand",
    }


def merge_annotations(base: dict, **overrides: Any) -> dict:
    """Mutate-and-return ``base`` with the given annotation sections replaced.

    Designed so panels can write::

        merge_annotations(
            st.session_state.setdefault(key, {}),
            pockets=pocket_annotations_from_df(df),
        )

    Sections not passed are preserved unchanged. Setting a section to
    ``None`` clears it.
    """
    for k, v in overrides.items():
        if v is None:
            base.pop(k, None)
        else:
            base[k] = v
    return base


_POCKET_PRODUCING_KINDS = ("find_pockets", "pipeline")
_CLUSTER_PRODUCING_KINDS = ("cluster", "pipeline")
_COMPLETED_STATUSES = ("completed", "SUCCESS", "success")


def _latest_completed(jobs: list[dict], kinds: Iterable[str]) -> Optional[dict]:
    """Pick the newest completed row whose ``kind`` is in ``kinds``."""
    kinds = tuple(kinds)
    matches = [
        row
        for row in jobs
        if row.get("kind") in kinds and row.get("status") in _COMPLETED_STATUSES
    ]
    if not matches:
        return None
    return max(matches, key=lambda r: r.get("last_updated") or "")


def _build_annotations(
    pockets_legacy_id: Optional[str],
    cluster_legacy_id: Optional[str],
    results_dir: Path,
) -> dict:
    """Pure CSV-reading body. Hashable args so it can be cached upstream."""
    out: dict = {}
    if pockets_legacy_id:
        csv = Path(results_dir) / pockets_legacy_id / "pockets" / "pockets.csv"
        if csv.exists():
            try:
                df = pd.read_csv(csv)
                pockets = pocket_annotations_from_df(df, top_n=5)
                if pockets:
                    out["pockets"] = pockets
            except Exception:
                pass
    if cluster_legacy_id:
        cluster_dir = Path(results_dir) / cluster_legacy_id / "pocket_clusters"
        reps_csv = cluster_dir / "cluster_representatives.csv"
        clustered_csv = cluster_dir / "pockets_clustered.csv"
        if reps_csv.exists():
            try:
                df_reps = pd.read_csv(reps_csv)
                df_clustered = None
                if clustered_csv.exists():
                    df_clustered = pd.read_csv(clustered_csv)
                    if "cluster" in df_clustered.columns:
                        df_clustered = df_clustered[df_clustered["cluster"] != -1]
                clusters = cluster_annotations_from_df(df_clustered, df_reps)
                if clusters:
                    out["clusters"] = clusters
            except Exception:
                pass
    return out


def _derive_cached_impl(
    pockets_legacy_id: Optional[str],
    cluster_legacy_id: Optional[str],
    pockets_last_updated: Optional[str],
    cluster_last_updated: Optional[str],
    results_dir_str: str,
) -> dict:
    """Cached derivation body. Cache key changes when source job ids OR their
    ``last_updated`` strings change — both rare events. Streamlit-side only;
    falls back to a plain call when Streamlit is unavailable (tests).
    """
    # The unused ``*_last_updated`` args are part of the cache key only —
    # they distinguish "same job, but its result CSV has been rewritten"
    # from "this is the same data we cached last call".
    del pockets_last_updated, cluster_last_updated
    return _build_annotations(pockets_legacy_id, cluster_legacy_id, Path(results_dir_str))


try:
    import streamlit as _st

    _derive_cached = _st.cache_data(show_spinner=False, ttl=600)(_derive_cached_impl)
except Exception:  # pragma: no cover — keeps tests importable without Streamlit
    _derive_cached = _derive_cached_impl


def derive_session_annotations(session_id, results_dir: Path) -> dict:
    """Build the data-derived sections of the annotation dict from the DB + disk.

    v2 Phase B B6. The viewer fragment in ``analysis_app.py`` calls this on
    every render. It reads :func:`failure_view.load_status_for_session`,
    picks the newest completed ``find_pockets`` / ``pipeline`` job (for
    pockets) and the newest completed ``cluster`` / ``pipeline`` job
    (for clusters), then reads the corresponding CSVs off ``results_dir``
    and produces the annotation sections via
    :func:`pocket_annotations_from_df` and
    :func:`cluster_annotations_from_df`.

    B11.7: the CSV-reading body is cached via ``@st.cache_data`` keyed on
    the source job ids + their ``last_updated``. Repeat calls in steady
    state return the cached dict in microseconds (vs ~10-100 ms for the
    cold CSV reads). The DB ``load_status_for_session`` query still
    runs on every call — it's cheap and the result drives the cache
    key.

    Args:
        session_id: ``Session.id`` UUID. ``None`` short-circuits to ``{}``.
        results_dir: Filesystem root where job results live (typically
            ``Config.RESULTS_DIR``). Injectable so tests don't depend on
            ``config.Config``.

    Returns:
        ``{"pockets": [...], "clusters": [...]}``; either section is
        omitted when its source job / CSV isn't available. Never raises;
        all I/O errors fall through silently (the viewer simply shows
        fewer annotations).
    """
    if session_id is None:
        return {}
    try:
        from failure_view import load_status_for_session
    except Exception:
        return {}

    jobs = load_status_for_session(session_id) or []
    pockets_job = _latest_completed(jobs, _POCKET_PRODUCING_KINDS)
    cluster_job = _latest_completed(jobs, _CLUSTER_PRODUCING_KINDS)

    return _derive_cached(
        pockets_job.get("legacy_id") if pockets_job else None,
        cluster_job.get("legacy_id") if cluster_job else None,
        pockets_job.get("last_updated") if pockets_job else None,
        cluster_job.get("last_updated") if cluster_job else None,
        str(results_dir),
    )
