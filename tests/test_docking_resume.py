"""Tests for tasks._load_done_pairs — the pair-level resume helper that
lets run_docking_task pick up where a killed-then-requeued task left off.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def _write_partial(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "docking_results.csv"
    pd.DataFrame(rows).to_csv(p, index=False)
    return p


class TestLoadDonePairs:
    def test_missing_file_returns_empty(self, tmp_path):
        from tasks import _load_done_pairs
        assert _load_done_pairs(str(tmp_path / "absent.csv")) == set()

    def test_empty_path_returns_empty(self):
        from tasks import _load_done_pairs
        assert _load_done_pairs("") == set()

    def test_two_pairs(self, tmp_path):
        from tasks import _load_done_pairs
        p = _write_partial(tmp_path, [
            {"receptor": "r1.pdb", "ligand": "lig_a", "affinity": -7.1},
            {"receptor": "r2.pdb", "ligand": "lig_a", "affinity": -6.5},
        ])
        out = _load_done_pairs(str(p))
        assert out == {("r1.pdb", "lig_a"), ("r2.pdb", "lig_a")}

    def test_dedupes_when_smina_emits_multiple_poses_per_pair(self, tmp_path):
        """Each pair often produces N pose rows (num_poses=10). The set
        collapses them to one key, which is exactly what we want — we
        skip the pair on resume regardless of how many poses it produced."""
        from tasks import _load_done_pairs
        p = _write_partial(tmp_path, [
            {"receptor": "r1.pdb", "ligand": "lig_a", "affinity": -7.1, "pose": 1},
            {"receptor": "r1.pdb", "ligand": "lig_a", "affinity": -6.8, "pose": 2},
            {"receptor": "r1.pdb", "ligand": "lig_a", "affinity": -6.5, "pose": 3},
            {"receptor": "r2.pdb", "ligand": "lig_b", "affinity": -8.2, "pose": 1},
        ])
        assert _load_done_pairs(str(p)) == {
            ("r1.pdb", "lig_a"),
            ("r2.pdb", "lig_b"),
        }

    def test_missing_columns_returns_empty(self, tmp_path):
        from tasks import _load_done_pairs
        # CSV with rows but neither 'receptor' nor 'ligand' columns.
        p = tmp_path / "docking_results.csv"
        pd.DataFrame({"x": [1, 2], "y": [3, 4]}).to_csv(p, index=False)
        assert _load_done_pairs(str(p)) == set()

    def test_partial_columns_returns_empty(self, tmp_path):
        """Has receptor but no ligand — caller can't disambiguate."""
        from tasks import _load_done_pairs
        p = _write_partial(tmp_path, [{"receptor": "r1.pdb", "score": -5.0}])
        assert _load_done_pairs(str(p)) == set()

    def test_empty_csv_returns_empty(self, tmp_path):
        from tasks import _load_done_pairs
        p = tmp_path / "docking_results.csv"
        p.write_text("receptor,ligand\n")
        assert _load_done_pairs(str(p)) == set()

    def test_corrupt_csv_returns_empty(self, tmp_path):
        """Non-CSV content (e.g. torn write or partial flush) — must not raise."""
        from tasks import _load_done_pairs
        p = tmp_path / "docking_results.csv"
        p.write_text("garbage,without,header\nrows that aren't,parseable")
        out = _load_done_pairs(str(p))
        # Either empty (best-effort) or some salvaged rows — must not raise.
        assert isinstance(out, set)

    def test_keys_are_strings(self, tmp_path):
        """Receptor and ligand cols can come back as object dtype; we
        normalise to str so the lookup in the dock loop is stable."""
        from tasks import _load_done_pairs
        p = _write_partial(tmp_path, [
            {"receptor": "r1.pdb", "ligand": 12345},  # numeric ligand stem
        ])
        out = _load_done_pairs(str(p))
        assert out == {("r1.pdb", "12345")}
