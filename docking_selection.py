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
