"""Shared docking-selection bucket (B11.2).

A small session-state-backed shopping cart of pockets the user has
chosen to dock. Populated from the Pockets table (multi-row select)
or the Cluster panel (per-pocket table + "Add all cluster
representatives" preset); consumed by the Docking panel as its
receptor source. The persistent bottom strip in ``analysis_app``
shows the count + Clear / Go-to-Docking actions.

Each bucket entry is a plain dict carrying every field the downstream
docking task needs:

    {
        "source_job_id": str,        # find_pockets job_id
        "File name": str,            # used for receptor PDB lookup
        "Frame_pocket_index": str,   # dedup key, e.g. "84_1"
        "Frame": int,
        "pocket_index": int,
        "probability": float,
        "residues": str,             # space-separated chain_resi tokens
        "cluster": int | None,       # None when added pre-clustering
    }

Dedup is on ``(source_job_id, Frame_pocket_index)`` so the same pocket
can't appear twice even if both Pockets-stage and Cluster-stage routes
get used. The bucket lives in ``st.session_state[_BUCKET_KEY]``.

``to_csv`` writes the bucket in the shape ``tasks.run_docking_task``
already reads (it only requires ``File name`` and ``residues`` —
see ``tasks.py:1084-1087``), so the existing task signature stays
unchanged.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd


_BUCKET_KEY = "docking_selected_pockets"


def _state():
    """Lazy import so importers that don't render UI don't touch Streamlit."""
    import streamlit as st
    return st.session_state


def current() -> list[dict]:
    """Return the bucket list (empty list when not initialized)."""
    return list(_state().get(_BUCKET_KEY, []))


def count() -> int:
    return len(_state().get(_BUCKET_KEY, []))


def _key(entry: dict) -> tuple[str, str]:
    return (str(entry.get("source_job_id", "")), str(entry.get("Frame_pocket_index", "")))


def add(pockets: Iterable[dict]) -> int:
    """Append the given pockets to the bucket, deduped by composite key.

    Returns the number of pockets actually added (already-present
    entries are skipped silently).
    """
    state = _state()
    bucket = list(state.get(_BUCKET_KEY, []))
    existing = {_key(e) for e in bucket}
    n_added = 0
    for p in pockets:
        k = _key(p)
        if k in existing or not k[1]:
            continue
        bucket.append(dict(p))
        existing.add(k)
        n_added += 1
    state[_BUCKET_KEY] = bucket
    return n_added


def remove(keys: Iterable[tuple[str, str]]) -> int:
    """Remove entries whose composite key is in ``keys``.

    Returns the number of removed entries.
    """
    state = _state()
    bucket = list(state.get(_BUCKET_KEY, []))
    drop = {(str(s), str(f)) for s, f in keys}
    kept = [e for e in bucket if _key(e) not in drop]
    state[_BUCKET_KEY] = kept
    return len(bucket) - len(kept)


def clear() -> None:
    _state()[_BUCKET_KEY] = []


def to_csv(path: Path) -> Path:
    """Write the bucket as a CSV the docking task can read.

    Columns: ``File name``, ``residues``, ``probability``, ``Frame``,
    ``pocket_index``, ``cluster``. Only ``File name`` and ``residues``
    are strictly required by ``tasks.run_docking_task``; the others are
    written for downstream visibility.

    Returns the path written. Raises ``ValueError`` if the bucket is
    empty (no useful CSV to produce).
    """
    bucket = current()
    if not bucket:
        raise ValueError("Docking bucket is empty — nothing to write.")
    df = pd.DataFrame(bucket)
    cols = [c for c in ("File name", "residues", "probability", "Frame",
                        "pocket_index", "cluster") if c in df.columns]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df[cols].to_csv(path, index=False)
    return path


def from_pockets_df(
    df: pd.DataFrame,
    *,
    source_job_id: str,
    cluster_override: Optional[int] = None,
) -> list[dict]:
    """Convert a DataFrame row subset into bucket-entry dicts.

    ``df`` is expected to have at least ``File name`` and ``residues``
    columns (the docking task minimums). ``Frame``, ``Frame_pocket_index``,
    ``pocket_index``, ``probability``, ``cluster`` are pulled when
    present.

    When ``cluster_override`` is supplied it sets each entry's
    ``cluster`` field — useful for the "all cluster representatives"
    preset that reads from ``cluster_representatives.csv`` (which
    carries the column already, but the override skips a fragile
    coercion).
    """
    out: list[dict] = []
    for _, row in df.iterrows():
        fpi = row.get("Frame_pocket_index")
        if fpi is None or (isinstance(fpi, float) and pd.isna(fpi)):
            # Fall back to building one from Frame + pocket_index.
            frame = row.get("Frame")
            pidx = row.get("pocket_index")
            if frame is None or pidx is None:
                continue
            fpi = f"{int(frame)}_{int(pidx)}"
        entry: dict[str, Any] = {
            "source_job_id": str(source_job_id),
            "File name": str(row.get("File name", "")),
            "Frame_pocket_index": str(fpi),
            "Frame": _coerce_int(row.get("Frame")),
            "pocket_index": _coerce_int(row.get("pocket_index")),
            "probability": _coerce_float(row.get("probability")),
            "residues": str(row.get("residues", "") or ""),
        }
        if cluster_override is not None:
            entry["cluster"] = int(cluster_override)
        else:
            cval = row.get("cluster")
            entry["cluster"] = (
                _coerce_int(cval) if cval is not None and not pd.isna(cval) else None
            )
        out.append(entry)
    return out


def _coerce_int(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _coerce_float(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0
