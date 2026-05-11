"""Tests for find_pockets_helpers — input validation + progress ranges."""
import pytest

from find_pockets_helpers import progress_ranges, validate_find_pockets_inputs


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
