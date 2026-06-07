"""Tests for docking_aggregation — pure-Python ensemble-aggregation helpers."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest


# ── best_per_pair ──────────────────────────────────────────────────────


class TestBestPerPair:
    def test_empty_input(self):
        from docking_aggregation import best_per_pair

        out = best_per_pair(pd.DataFrame())
        assert out.empty

    def test_missing_columns_raises(self):
        from docking_aggregation import best_per_pair

        df = pd.DataFrame({"ligand": ["L1"], "score": [-5.0]})
        with pytest.raises(KeyError, match="missing"):
            best_per_pair(df)

    def test_collapses_multiple_poses_keeps_min(self):
        from docking_aggregation import best_per_pair

        df = pd.DataFrame({
            "ligand":   ["L1", "L1", "L1", "L2"],
            "receptor": ["R1", "R1", "R2", "R1"],
            "affinity (kcal/mol)": [-5.0, -7.5, -6.0, -8.0],
        })
        grid = best_per_pair(df)
        assert grid.loc["L1", "R1"] == -7.5   # min of -5 and -7.5
        assert grid.loc["L1", "R2"] == -6.0
        assert grid.loc["L2", "R1"] == -8.0
        # Missing (L2, R2) pair → NaN.
        assert math.isnan(grid.loc["L2", "R2"])

    def test_keys_coerced_to_string(self):
        """Numeric ligand/receptor IDs in CSV roundtrips should land as str
        index/columns so downstream `.loc[]` lookups don't surprise."""
        from docking_aggregation import best_per_pair

        df = pd.DataFrame({
            "ligand":   [1, 2],
            "receptor": [10, 20],
            "affinity (kcal/mol)": [-3.0, -4.0],
        })
        grid = best_per_pair(df)
        assert list(grid.index) == ["1", "2"]
        assert list(grid.columns) == ["10", "20"]


# ── aggregate_ensemble ─────────────────────────────────────────────────


class TestBestPosesPerPair:
    """Round-trip the helper to confirm it preserves every per-pose column."""

    def test_empty_input(self):
        from docking_aggregation import best_poses_per_pair

        out = best_poses_per_pair(pd.DataFrame())
        assert out.empty

    def test_missing_columns_raises(self):
        from docking_aggregation import best_poses_per_pair

        with pytest.raises(KeyError, match="missing"):
            best_poses_per_pair(pd.DataFrame({"ligand": ["L1"], "x": [1]}))

    def test_keeps_full_row_for_min_pose(self):
        from docking_aggregation import best_poses_per_pair

        # Two poses for (L1, R1); the best one carries pose_index=5 and a
        # custom output_sdf. The helper must surface THAT row, not just
        # the affinity value.
        df = pd.DataFrame({
            "ligand":   ["L1", "L1", "L1"],
            "receptor": ["R1", "R1", "R2"],
            "affinity (kcal/mol)": [-5.0, -8.5, -6.0],
            "pose_index": [1, 5, 1],
            "output_sdf": ["a.sdf", "b.sdf", "c.sdf"],
        })
        best = best_poses_per_pair(df)
        assert len(best) == 2
        l1r1 = best[(best.ligand == "L1") & (best.receptor == "R1")].iloc[0]
        assert l1r1["affinity (kcal/mol)"] == -8.5
        assert l1r1["pose_index"] == 5
        assert l1r1["output_sdf"] == "b.sdf"


class TestAggregateEnsemble:
    def _grid(self):
        # Hand-computed values:
        # L1: [-5,  -6,  -7]   → mean -6,    median -6,   best -7
        # L2: [-4,  NaN, -10]  → mean -7,    median -7,   best -10
        # L3: [-2,  -3,  -4]   → mean -3,    median -3,   best -4
        return pd.DataFrame(
            {
                "R1": [-5.0, -4.0, -2.0],
                "R2": [-6.0, float("nan"), -3.0],
                "R3": [-7.0, -10.0, -4.0],
            },
            index=["L1", "L2", "L3"],
        )

    def test_empty_grid_returns_empty_with_columns(self):
        from docking_aggregation import aggregate_ensemble

        out = aggregate_ensemble(pd.DataFrame())
        assert list(out.columns) == ["mean", "median", "best", "ecr"]
        assert out.empty

    def test_mean_median_best_match_hand_computed(self):
        from docking_aggregation import aggregate_ensemble

        out = aggregate_ensemble(self._grid())
        assert out.loc["L1", "mean"] == pytest.approx(-6.0)
        assert out.loc["L1", "median"] == pytest.approx(-6.0)
        assert out.loc["L1", "best"] == pytest.approx(-7.0)
        assert out.loc["L2", "mean"] == pytest.approx(-7.0)  # skipna=True
        assert out.loc["L2", "median"] == pytest.approx(-7.0)
        assert out.loc["L2", "best"] == pytest.approx(-10.0)
        assert out.loc["L3", "mean"] == pytest.approx(-3.0)
        assert out.loc["L3", "median"] == pytest.approx(-3.0)
        assert out.loc["L3", "best"] == pytest.approx(-4.0)

    def test_index_order_preserved(self):
        from docking_aggregation import aggregate_ensemble

        out = aggregate_ensemble(self._grid())
        assert list(out.index) == ["L1", "L2", "L3"]


# ── ecr_scores ─────────────────────────────────────────────────────────


class TestEcrScores:
    def test_empty_grid(self):
        from docking_aggregation import ecr_scores

        s = ecr_scores(pd.DataFrame())
        assert s.empty

    def test_invalid_sigma_raises(self):
        from docking_aggregation import ecr_scores

        with pytest.raises(ValueError, match="sigma"):
            ecr_scores(pd.DataFrame({"R1": [-1.0]}, index=["L1"]), sigma=0)

    def test_lower_affinity_yields_higher_ecr(self):
        from docking_aggregation import ecr_scores

        grid = pd.DataFrame(
            {"R1": [-9.0, -5.0, -1.0], "R2": [-8.0, -4.0, 0.0]},
            index=["L1", "L2", "L3"],
        )
        s = ecr_scores(grid, sigma=1.0)
        # L1 is rank-1 on both columns → highest ECR.
        assert s.loc["L1"] > s.loc["L2"] > s.loc["L3"]

    def test_hand_computed_with_explicit_sigma(self):
        """Verify the formula directly on a tiny 2×2 grid."""
        from docking_aggregation import ecr_scores

        # L1: ranks [1, 2]; L2: ranks [2, 1]; σ = 1.
        grid = pd.DataFrame(
            {"R1": [-5.0, -3.0], "R2": [-1.0, -4.0]},
            index=["L1", "L2"],
        )
        s = ecr_scores(grid, sigma=1.0)
        expected_L1 = math.exp(-1) + math.exp(-2)
        expected_L2 = math.exp(-2) + math.exp(-1)
        assert s.loc["L1"] == pytest.approx(expected_L1)
        assert s.loc["L2"] == pytest.approx(expected_L2)
        # By symmetry the two scores tie.
        assert s.loc["L1"] == pytest.approx(s.loc["L2"])

    def test_nan_cell_treated_as_rank_n_plus_one(self):
        """A missing (ligand, receptor) score should contribute almost nothing."""
        from docking_aggregation import ecr_scores

        grid = pd.DataFrame(
            {"R1": [-5.0, -4.0, -3.0],
             "R2": [-5.0, float("nan"), -3.0]},
            index=["L1", "L2", "L3"],
        )
        s = ecr_scores(grid, sigma=1.0)
        # L2's R2 cell is NaN → rank N+1 = 4 → exp(-4) ≈ 0.0183.
        # L2's R1: ascending rank is 2 (-4 between -5 and -3) → exp(-2).
        expected_L2 = math.exp(-2) + math.exp(-4)
        assert s.loc["L2"] == pytest.approx(expected_L2)

    def test_ties_share_min_rank(self):
        """Two identical affinities for the same receptor share rank 1."""
        from docking_aggregation import ecr_scores

        grid = pd.DataFrame(
            {"R1": [-5.0, -5.0, -3.0]},
            index=["L1", "L2", "L3"],
        )
        s = ecr_scores(grid, sigma=1.0)
        # method="min" → L1 and L2 both rank 1, L3 ranks 3.
        assert s.loc["L1"] == pytest.approx(math.exp(-1))
        assert s.loc["L2"] == pytest.approx(math.exp(-1))
        assert s.loc["L3"] == pytest.approx(math.exp(-3))

    def test_default_sigma_scales_with_pool_size(self):
        """σ defaults to max(1, N/10) — verify N=20 produces σ=2."""
        from docking_aggregation import ecr_scores

        n = 20
        # L0 has affinity 0 (worst), L19 has -19 (most-negative, best).
        grid = pd.DataFrame(
            {"R1": -np.arange(n, dtype=float)},
            index=[f"L{i}" for i in range(n)],
        )
        s = ecr_scores(grid)
        # The best ligand (L19) is the rank-1 entry → exp(-1/σ) = exp(-0.5).
        assert s.loc["L19"] == pytest.approx(math.exp(-1 / 2.0))
        # The worst ligand (L0) is rank N=20 → exp(-10), much smaller.
        assert s.loc["L0"] == pytest.approx(math.exp(-10.0))
