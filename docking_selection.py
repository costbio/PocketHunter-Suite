"""Helpers for the docking selection UX.

These helpers are pure (no Streamlit, no I/O for the unit-testable pieces)
so they can be tested without booting the app.

- ``box_dims_from_minmax`` — turn axis-aligned bounding-box corners into
  per-axis dimensions with optional padding.
- ``max_box_dims`` — take the per-axis max of several box-dim tuples, used
  when the user selects multiple PDBs with different pocket footprints.
- ``box_size_for_pocket`` — convenience wrapper that calls
  ``step4_docking.calc_box`` and returns the padded dimensions for a PDB
  file. Not unit-tested because it touches ProDy + the filesystem.
- ``summarize_selection`` — render a one-line "Targeting N clusters: …"
  summary for the docking page header, using the spatial-descriptor
  helper from ``cluster_labels``.
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

import pandas as pd


def box_dims_from_minmax(
    box_min: Sequence[float],
    box_max: Sequence[float],
    padding: float = 4.0,
) -> tuple[float, float, float]:
    """Compute (sx, sy, sz) from axis-aligned bounding-box corners + padding.

    Pure math, no I/O. Accepts tuples, lists, or numpy arrays.
    """
    sx = float(box_max[0] - box_min[0]) + 2 * padding
    sy = float(box_max[1] - box_min[1]) + 2 * padding
    sz = float(box_max[2] - box_min[2]) + 2 * padding
    return sx, sy, sz


def max_box_dims(
    dims_list: Iterable[tuple[float, float, float]],
) -> Optional[tuple[float, float, float]]:
    """Take the per-axis max of several (sx, sy, sz) box-dim tuples.

    Returns None for an empty iterable so callers can fall back to a default.
    """
    dims_list = list(dims_list)
    if not dims_list:
        return None
    sx = max(d[0] for d in dims_list)
    sy = max(d[1] for d in dims_list)
    sz = max(d[2] for d in dims_list)
    return sx, sy, sz


def box_size_for_pocket(
    pdb_path: str,
    residues: str,
    padding: float = 4.0,
) -> tuple[float, float, float]:
    """Compute box X/Y/Z dimensions for a pocket in a PDB file.

    Thin wrapper around ``step4_docking.calc_box`` that adds padding and
    returns just the box size (not the center). Caller is responsible for
    handling exceptions if the PDB or residues are malformed.
    """
    # Local import: step4_docking depends on ProDy/OpenBabel which are heavy
    # and should not be imported at module load time of pure helpers.
    from step4_docking import calc_box
    _, box_min, box_max = calc_box(pdb_path, residues)
    return box_dims_from_minmax(box_min, box_max, padding=padding)


def auto_box_for_selection(
    df_reps: pd.DataFrame,
    job_id: str,
    selected_clusters: Optional[Iterable[int]] = None,
    *,
    padding: float = 4.0,
    extra_pdb_source_job_id: Optional[str] = None,
) -> Optional[tuple[float, float, float, str]]:
    """Compute ``(sx, sy, sz, label)`` for the top-probability representative.

    Filtering: if ``selected_clusters`` is given, only those clusters are
    considered. Otherwise the top probability across all of ``df_reps`` wins.

    PDB lookup: tries ``results/<job_id>/pdbs/<file>`` then
    ``results/<job_id>/pocket_clusters/<file>``. If ``extra_pdb_source_job_id``
    is supplied (when the docking panel knows about an upstream extract
    job), that location is tried first.

    Returns ``None`` if any step fails (no rep matches the filter, missing
    PDB, residues unparseable, etc.) — caller falls back to manual sliders.
    """
    try:
        if df_reps is None or len(df_reps) == 0:
            return None
        if "residues" not in df_reps.columns:
            return None

        sub = df_reps
        if selected_clusters is not None:
            ids = [int(c) for c in selected_clusters]
            if not ids:
                return None
            if "cluster" not in df_reps.columns:
                return None
            sub = df_reps[df_reps["cluster"].isin(ids)]
        if sub.empty:
            return None

        if "probability" in sub.columns:
            top = sub.sort_values("probability", ascending=False).iloc[0]
        else:
            top = sub.iloc[0]
        residues = str(top.get("residues", "") or "")
        if not residues.strip():
            return None

        file_name = str(top.get("File name", "") or "")
        pdb_path = _resolve_rep_pdb_path(file_name, job_id, extra_pdb_source_job_id)
        if not pdb_path:
            return None

        sx, sy, sz = box_size_for_pocket(pdb_path, residues, padding=padding)
        cluster_id = (
            int(top.get("cluster", 0)) if "cluster" in sub.columns and pd.notna(top.get("cluster", 0))
            else 0
        )
        return (round(sx, 1), round(sy, 1), round(sz, 1), f"Cluster {cluster_id}")
    except Exception:
        return None


def _resolve_rep_pdb_path(
    file_name: str,
    job_id: str,
    extra_source: Optional[str] = None,
) -> Optional[str]:
    """Locate a representative's PDB file across the conventional output dirs.

    Strips the p2rank ``_predictions`` suffix and ensures the ``.pdb`` extension,
    then tries ``<source>/pdbs/`` and ``<source>/pocket_clusters/`` for every
    source in priority order: ``extra_source`` (if given), then ``job_id``.
    """
    import os
    from config import Config
    results_dir = str(Config.RESULTS_DIR)

    pdb_name = file_name.replace('_predictions', '') if '_predictions' in file_name else file_name
    if not pdb_name.endswith('.pdb'):
        pdb_name += '.pdb'

    sources = [s for s in (extra_source, job_id) if s]
    for source in sources:
        for subdir in ('pdbs', 'pocket_clusters'):
            cand = os.path.join(results_dir, source, subdir, pdb_name)
            if os.path.exists(cand):
                return cand
    return None


def summarize_selection(
    cluster_ids: Iterable[int],
    df_reps: pd.DataFrame,
) -> str:
    """One-line human summary of which clusters are being docked.

    Example output::

        Targeting 3 clusters: Cluster 0 · A · 150–154; Cluster 2 · A · 420–421 | B · 35–36; Cluster 3 · B · 110–111
    """
    # Local import to avoid a hard cycle if this module is imported before
    # cluster_labels has been re-loaded.
    from cluster_labels import describe_cluster_spatially

    ids = sorted(int(c) for c in cluster_ids)
    if not ids:
        return "No clusters selected"

    parts: list[str] = []
    for cid in ids:
        match = df_reps[df_reps.get("cluster", pd.Series(dtype=object)) == cid] if "cluster" in df_reps.columns else df_reps.iloc[0:0]
        if match.empty:
            parts.append(f"Cluster {cid} (no representative found)")
            continue
        row = match.iloc[0]
        residues = row.get("residues", "") if hasattr(row, "get") else ""
        spatial = describe_cluster_spatially(residues)
        parts.append(f"Cluster {cid} · {spatial}")

    plural = "s" if len(ids) != 1 else ""
    return f"Targeting {len(ids)} cluster{plural}: " + "; ".join(parts)
