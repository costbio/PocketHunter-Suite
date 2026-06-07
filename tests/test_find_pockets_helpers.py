"""Tests for find_pockets_helpers — input validation + progress ranges."""
import pytest

from find_pockets_helpers import (
    progress_ranges,
    validate_find_pockets_inputs,
    write_pdb_list_for_detect,
)


class TestValidateFindPocketsInputs:
    def test_trajectory_mode(self):
        assert validate_find_pockets_inputs("a.xtc", "b.pdb", None) == "trajectory"

    def test_pdb_dir_mode(self):
        assert validate_find_pockets_inputs(None, None, "/tmp/pdbs") == "pdb_dir"

    def test_partial_trajectory_xtc_only(self):
        with pytest.raises(ValueError, match="BOTH"):
            validate_find_pockets_inputs("a.xtc", None, None)

    def test_partial_trajectory_topology_only(self):
        with pytest.raises(ValueError, match="BOTH"):
            validate_find_pockets_inputs(None, "b.pdb", None)

    def test_mixed_modes_rejected(self):
        with pytest.raises(ValueError, match="mix"):
            validate_find_pockets_inputs("a.xtc", "b.pdb", "/tmp/pdbs")

    def test_no_inputs_rejected(self):
        with pytest.raises(ValueError, match="No inputs"):
            validate_find_pockets_inputs(None, None, None)

    def test_empty_strings_treated_as_missing(self):
        # Streamlit text inputs often return "" rather than None.
        with pytest.raises(ValueError, match="No inputs"):
            validate_find_pockets_inputs("", "", "")


class TestProgressRanges:
    def test_trajectory_splits_evenly(self):
        extract, detect = progress_ranges("trajectory")
        assert extract == (0, 50)
        assert detect == (50, 100)

    def test_pdb_dir_gives_detect_full_ramp(self):
        extract, detect = progress_ranges("pdb_dir")
        assert extract == (0, 0)
        assert detect == (0, 100)

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError):
            progress_ranges("bogus")


class TestWritePdbListForDetect:
    def test_writes_basenames_one_per_line(self, tmp_path):
        for name in ("frame_001.pdb", "frame_002.pdb", "frame_003.pdb"):
            (tmp_path / name).write_text("ATOM\n")
        out = write_pdb_list_for_detect(tmp_path)
        assert out == tmp_path / "pdb_list.ds"
        lines = out.read_text().splitlines()
        assert lines == ["frame_001.pdb", "frame_002.pdb", "frame_003.pdb"]

    def test_ignores_non_pdb_files(self, tmp_path):
        (tmp_path / "a.pdb").write_text("")
        (tmp_path / "ignore.txt").write_text("")
        (tmp_path / "also.gro").write_text("")
        out = write_pdb_list_for_detect(tmp_path)
        assert out.read_text().splitlines() == ["a.pdb"]

    def test_empty_dir_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="No .pdb"):
            write_pdb_list_for_detect(tmp_path)

    def test_accepts_string_path(self, tmp_path):
        (tmp_path / "a.pdb").write_text("")
        out = write_pdb_list_for_detect(str(tmp_path))
        assert out.exists()
