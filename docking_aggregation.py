"""Pure aggregation helpers for the docking score grid.

Used by ``panels/docking.py`` to rank ligands against an ensemble of
receptor conformations. Kept Streamlit-free so the maths is
unit-testable in isolation.

Four ensemble-aggregation methods are surfaced to the user:

* ``mean`` — arithmetic mean of per-receptor best affinities. The
  current PocketHunter default. Suitable for minimized / clustered
  ensembles where each receptor carries roughly equal weight
  (Paulsen & Anderson, *J. Chem. Inf. Model.* 2009,
  https://doi.org/10.1021/ci9003078).
* ``median`` — robust to a single mis-scoring receptor pulling the
  mean around.
* ``best`` — most-negative affinity across the ensemble. The
  Relaxed-Complex-Scheme observable (Lin, Perryman, Schames, McCammon,
  *JACS* 2002, https://doi.org/10.1021/ja0260162); surfaces rare
  cryptic-site binding.
* ``ecr`` — Exponential Consensus Ranking (Palacio-Rodríguez et al.,
  *Sci. Rep.* 2019, https://doi.org/10.1038/s41598-019-41594-3).
  Rank-based, unit-free, robust to scoring-function scale drift across
  receptors. Higher = better.

All three functions are NaN-aware: missing (ligand, receptor) pairs are
treated consistently (skipped in mean / median; ignored in ``best``;
ranked worst in ECR).
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd


DEFAULT_AFFINITY_COLUMN = "affinity (kcal/mol)"


def best_per_pair(
    long_df: pd.DataFrame,
    *,
    aff_col: str = DEFAULT_AFFINITY_COLUMN,
) -> pd.DataFrame:
    """Collapse a long-format docking results CSV into a ligand × receptor grid.

    Each cell is the lowest (most-negative) ``aff_col`` value across
    every pose of that ``(ligand, receptor)`` pair. Missing pairs are
    NaN. Returns a DataFrame indexed by ligand, columns are receptor
    names; both axes carry the natural pandas insertion order (callers
    that want a specific order reindex).
    """
    if long_df is None or long_df.empty:
        return pd.DataFrame(dtype="float64")
    needed = {"ligand", "receptor", aff_col}
    missing = needed - set(long_df.columns)
    if missing:
        raise KeyError(f"best_per_pair: missing columns {missing}")
    # Cast key columns to str so the pivot index/columns are stable
    # across the pandas / Streamlit boundary (the CSV roundtrip may
    # leave them as object dtype).
    df = long_df[["ligand", "receptor", aff_col]].copy()
    df["ligand"] = df["ligand"].astype(str)
    df["receptor"] = df["receptor"].astype(str)
    grid = df.pivot_table(
        index="ligand",
        columns="receptor",
        values=aff_col,
        aggfunc="min",
    )
    grid.columns.name = None
    return grid.astype("float64")


def best_poses_per_pair(
    long_df: pd.DataFrame,
    *,
    aff_col: str = DEFAULT_AFFINITY_COLUMN,
) -> pd.DataFrame:
    """Return one row per (ligand, receptor) pair — the lowest-affinity pose.

    Sibling of :func:`best_per_pair`: same groupby + idxmin primitive,
    but preserves every column of the source row (pose number,
    ``ligand_name``, ``receptor_pdb_path``, ``output_sdf``, …) instead
    of collapsing to the affinity scalar.

    Used by the CSV "best poses" download paths in
    ``panels/docking.py`` (``_results_zip_cached`` + the Downloads
    expander button). Replaces three near-identical inline patterns
    with a single helper.
    """
    if long_df is None or long_df.empty:
        return long_df.iloc[0:0].copy() if long_df is not None else pd.DataFrame()
    needed = {"ligand", "receptor", aff_col}
    missing = needed - set(long_df.columns)
    if missing:
        raise KeyError(f"best_poses_per_pair: missing columns {missing}")
    return long_df.loc[long_df.groupby(["ligand", "receptor"])[aff_col].idxmin()]


def aggregate_ensemble(grid: pd.DataFrame) -> pd.DataFrame:
    """Return per-ligand ensemble-aggregation columns.

    Output DataFrame is indexed by ligand (same order as input) with
    four float columns: ``mean``, ``median``, ``best``, ``ecr``. Empty
    input → empty output with the expected column shape.
    """
    cols = ["mean", "median", "best", "ecr"]
    if grid is None or grid.empty:
        return pd.DataFrame(columns=cols, dtype="float64")
    out = pd.DataFrame(index=grid.index, columns=cols, dtype="float64")
    out["mean"] = grid.mean(axis=1, skipna=True)
    out["median"] = grid.median(axis=1, skipna=True)
    out["best"] = grid.min(axis=1, skipna=True)
    out["ecr"] = ecr_scores(grid)
    return out


def ecr_scores(
    grid: pd.DataFrame,
    *,
    sigma: Optional[float] = None,
) -> pd.Series:
    """Exponential Consensus Ranking score per ligand.

    For each receptor (column), rank ligands ascending by affinity
    (lowest / most-negative → rank 1). ``ECR(ligand) = Σ_r exp(−rank_r / σ)``.

    NaN cells get rank ``n_ligands + 1`` so they contribute
    ``exp(−(N+1)/σ) ≈ 0`` — a docking failure punishes the ligand on
    that receptor without nuking its score across the rest.

    ``sigma`` defaults to ``max(1.0, n_ligands / 10)`` per the paper's
    recommendation: makes the top ~10 % of each ranking carry most of
    the weight, regardless of ligand-pool size.

    Returns a Series indexed by the grid's ligand axis. Higher is
    better — the panel inverts the sort.
    """
    if grid is None or grid.empty:
        return pd.Series(dtype="float64")
    n = len(grid.index)
    if sigma is None:
        sigma = max(1.0, n / 10.0)
    if sigma <= 0:
        raise ValueError(f"sigma must be > 0, got {sigma}")

    # Rank each column ascending; ties share the lower rank
    # (method="min"), and NaN cells get rank N+1 so they barely
    # contribute via the exp decay.
    ranks = grid.rank(axis=0, method="min", ascending=True, na_option="bottom")
    # ``na_option="bottom"`` ranks NaN as if last, but they then receive
    # a position somewhere in [observed_max + 1 .. N]; force every NaN
    # cell to N+1 explicitly so the contribution is uniform regardless
    # of how many real values shared the column.
    ranks = ranks.where(grid.notna(), other=float(n + 1))

    decay = np.exp(-ranks.to_numpy() / sigma)
    return pd.Series(decay.sum(axis=1), index=grid.index, dtype="float64")
