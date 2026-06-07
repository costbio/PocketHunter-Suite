"""Tests for the cluster panel's hierarchical-rep helpers + tasks-side
stderr extraction (Phase D cluster-failure batch + per-parent K cut).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest


# ── _medoid_index ───────────────────────────────────────────────────────


class TestMedoidIndex:
    def test_empty(self):
        from panels.cluster import _medoid_index
        assert _medoid_index(np.zeros((0, 5))) == -1

    def test_single_row(self):
        from panels.cluster import _medoid_index
        assert _medoid_index(np.array([[1, 0, 1, 1, 0]])) == 0

    def test_central_row_wins(self):
        from panels.cluster import _medoid_index

        rows = np.array([
            [1, 1, 1, 1, 1],   # extreme A
            [1, 1, 0, 0, 0],   # middle (centroid)
            [0, 0, 0, 0, 0],   # extreme B
        ])
        assert _medoid_index(rows) == 1


# ── _hierarchical_parents ───────────────────────────────────────────────


class TestHierarchicalParents:
    def test_empty_dir(self, tmp_path):
        from panels.cluster import _hierarchical_parents
        assert _hierarchical_parents(str(tmp_path)) == []

    def test_two_files_sorted(self, tmp_path):
        from panels.cluster import _hierarchical_parents
        (tmp_path / "cluster_5_hierarchical.csv").write_text("a,b\n1,2\n")
        (tmp_path / "cluster_0_hierarchical.csv").write_text("a,b\n1,2\n")
        # Decoy: filename doesn't match regex.
        (tmp_path / "cluster_X_hierarchical.csv").write_text("a,b\n1,2\n")
        (tmp_path / "cluster_representatives.csv").write_text("a,b\n1,2\n")
        assert _hierarchical_parents(str(tmp_path)) == [0, 5]


# ── _recut_hierarchical ─────────────────────────────────────────────────


def _make_parent_csv(tmp_path: Path, n_rows: int, *, residue_groups: list[list[int]] | None = None) -> Path:
    """Hand-built parent hierarchical CSV.

    ``residue_groups`` controls the binary residue fingerprints — each
    inner list is the set of residue indices (1..8) that are "1" for
    that row. Length must equal ``n_rows``. If omitted, every row has
    a distinct singleton bit so each row is its own neighborhood.
    """
    if residue_groups is None:
        residue_groups = [[i + 1] for i in range(n_rows)]
    assert len(residue_groups) == n_rows
    rows = []
    for i, bits in enumerate(residue_groups):
        row = {
            "Frame_pocket_index": f"{i}_1",
            "File name": f"f{i}.pdb",
            "Frame": i,
            "pocket_index": 1,
            "probability": 0.7 + i * 0.005,
            "residues": " ".join(f"R{b}" for b in bits),
            "cluster": 0,
            "hierarchical_cluster": i,  # PocketHunter's over-split labels
        }
        row.update({f"A_{j}": int(j in bits) for j in range(1, 9)})
        rows.append(row)
    path = tmp_path / "cluster_0_hierarchical.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


class TestRecutHierarchical:
    def test_k1_returns_single_row(self, tmp_path):
        from panels.cluster import _recut_hierarchical
        path = _make_parent_csv(tmp_path, n_rows=4)
        out = _recut_hierarchical(str(path), 1)
        assert len(out) == 1

    def test_k_equals_n_returns_n_medoids(self, tmp_path):
        from panels.cluster import _recut_hierarchical
        path = _make_parent_csv(tmp_path, n_rows=4)
        # Distinct singleton fingerprints → fcluster(maxclust=4) gives 4 leaves
        # → 4 medoids each equal to its only member.
        out = _recut_hierarchical(str(path), 4)
        assert len(out) == 4
        # All four input rows are represented (just check Frames).
        assert set(out["Frame"].tolist()) == {0, 1, 2, 3}

    def test_k_larger_than_n_clamps(self, tmp_path):
        from panels.cluster import _recut_hierarchical
        path = _make_parent_csv(tmp_path, n_rows=3)
        out = _recut_hierarchical(str(path), 10)
        assert len(out) == 3

    def test_k2_groups_neighbors(self, tmp_path):
        """Two natural groups → fcluster(maxclust=2) returns one medoid each."""
        from panels.cluster import _recut_hierarchical
        # Two clear clusters: rows 0-2 share bits 1,2; rows 3-5 share bits 7,8.
        path = _make_parent_csv(
            tmp_path,
            n_rows=6,
            residue_groups=[
                [1, 2], [1, 2, 3], [1, 2],
                [7, 8], [7, 8], [6, 7, 8],
            ],
        )
        out = _recut_hierarchical(str(path), 2)
        assert len(out) == 2
        # One medoid per natural group.
        frames = sorted(out["Frame"].tolist())
        # First group's medoids draw from frames {0,1,2}; second from {3,4,5}.
        assert frames[0] in {0, 1, 2}
        assert frames[1] in {3, 4, 5}

    def test_single_row_input(self, tmp_path):
        from panels.cluster import _recut_hierarchical
        path = _make_parent_csv(tmp_path, n_rows=1)
        # K=1 and K=5 should both yield the single row.
        for k in (1, 5):
            out = _recut_hierarchical(str(path), k)
            assert len(out) == 1
            assert out["Frame"].iloc[0] == 0


# ── _extract_last_exception_line (tasks.py) ─────────────────────────────


class TestExtractLastExceptionLine:
    def test_none_input(self):
        from tasks import _extract_last_exception_line
        assert _extract_last_exception_line(None) is None
        assert _extract_last_exception_line("") is None

    def test_no_exception_lines(self):
        from tasks import _extract_last_exception_line
        assert _extract_last_exception_line("just info\nnothing here\n") is None

    def test_extracts_scipy_value_error_from_dbscan_dump(self):
        """The exact failure pattern that bit the trypsin demo."""
        from tasks import _extract_last_exception_line

        stderr = (
            "INFO - Optimizing round 174/175 ...\n"
            "INFO - Optimizing round 175/175 ...\n"
            "INFO - Best Silhouette Score: 0.09\n"
            "INFO - Performing hierarchical clustering ...\n"
            "Traceback (most recent call last):\n"
            "  File \"/app/PocketHunter/pockethunter.py\", line 852, in <module>\n"
            "    main()\n"
            "  File \".../scipy/spatial/distance.py\", line 2742, in num_obs_y\n"
            "    raise ValueError(\"The number of observations cannot be \"\n"
            "ValueError: The number of observations cannot be determined "
            "on an empty distance matrix.\n"
        )
        out = _extract_last_exception_line(stderr)
        assert out is not None
        assert out.startswith("ValueError: ")
        assert "empty distance matrix" in out

    def test_picks_last_when_multiple(self):
        from tasks import _extract_last_exception_line

        stderr = (
            "RuntimeError: first\n"
            "some other output\n"
            "TypeError: second one wins\n"
        )
        assert _extract_last_exception_line(stderr) == "TypeError: second one wins"
