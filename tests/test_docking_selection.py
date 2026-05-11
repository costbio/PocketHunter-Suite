"""Tests for the docking_selection helper module."""
import numpy as np
import pandas as pd
import pytest

from docking_selection import (
    box_dims_from_minmax,
    max_box_dims,
    summarize_selection,
)


class TestBoxDimsFromMinmax:
    def test_zero_padding(self):
        # 10×20×30 box, no padding
        result = box_dims_from_minmax((0, 0, 0), (10, 20, 30), padding=0.0)
        assert result == (10.0, 20.0, 30.0)

    def test_with_padding(self):
        # 10×20×30 box + 4Å padding each side → 18×28×38
        result = box_dims_from_minmax((0, 0, 0), (10, 20, 30), padding=4.0)
        assert result == (18.0, 28.0, 38.0)

    def test_negative_coords(self):
        result = box_dims_from_minmax((-5, -10, -2), (5, 10, 8), padding=0.0)
        assert result == (10.0, 20.0, 10.0)

    def test_accepts_ndarrays(self):
        bmin = np.array([1.5, 2.5, 3.5])
        bmax = np.array([11.5, 22.5, 33.5])
        result = box_dims_from_minmax(bmin, bmax, padding=2.0)
        assert result == (14.0, 24.0, 34.0)

    def test_floats_preserved(self):
        result = box_dims_from_minmax((0.0, 0.0, 0.0), (10.5, 20.5, 30.5), padding=1.5)
        assert result == (13.5, 23.5, 33.5)


class TestMaxBoxDims:
    def test_single_dim(self):
        assert max_box_dims([(10.0, 20.0, 30.0)]) == (10.0, 20.0, 30.0)

    def test_multiple_takes_max_per_axis(self):
        dims = [(10.0, 25.0, 30.0), (15.0, 20.0, 35.0), (12.0, 22.0, 28.0)]
        # max per axis: 15, 25, 35
        assert max_box_dims(dims) == (15.0, 25.0, 35.0)

    def test_empty_returns_none(self):
        assert max_box_dims([]) is None


class TestSummarizeSelection:
    @staticmethod
    def _reps():
        return pd.DataFrame([
            {"cluster": 0, "residues": "A_150 A_151 A_152 A_153 A_154"},
            {"cluster": 1, "residues": "A_310 A_311 A_320 A_321"},
            {"cluster": 2, "residues": "A_420 A_421 B_35 B_36"},
            {"cluster": 3, "residues": "B_110 B_111"},
        ])

    def test_empty_cluster_list(self):
        assert summarize_selection([], self._reps()) == "No clusters selected"

    def test_single_cluster(self):
        out = summarize_selection([0], self._reps())
        assert out.startswith("Targeting 1 cluster:")
        assert "Cluster 0" in out
        assert "A · 150–154" in out

    def test_multiple_clusters(self):
        out = summarize_selection([0, 2], self._reps())
        assert out.startswith("Targeting 2 clusters:")
        assert "Cluster 0" in out
        assert "Cluster 2" in out
        # Multi-chain spatial descriptor visible
        assert "A · 420–421" in out or "B · 35–36" in out

    def test_missing_cluster_in_reps(self):
        out = summarize_selection([0, 99], self._reps())
        assert "Cluster 0" in out
        assert "Cluster 99" in out
        assert "no representative" in out.lower()

    def test_unsorted_input_sorted_output(self):
        out = summarize_selection([3, 0, 1], self._reps())
        # Should list clusters in ascending order regardless of input order
        c0 = out.index("Cluster 0")
        c1 = out.index("Cluster 1")
        c3 = out.index("Cluster 3")
        assert c0 < c1 < c3

    def test_handles_residues_field_missing(self):
        df = pd.DataFrame([{"cluster": 0}])  # no residues column
        out = summarize_selection([0], df)
        assert "Cluster 0" in out  # falls through gracefully
