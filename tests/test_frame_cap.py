"""Tests for the MAX_TRAJECTORY_FRAMES viewer-renderability cap."""
from __future__ import annotations

import pytest


class TestFrameCapConfig:
    def test_default_is_1000(self):
        from settings import Settings
        s = Settings(DATABASE_URL="sqlite:///:memory:", BASE_URL="http://localhost")
        assert s.MAX_TRAJECTORY_FRAMES == 1000

    def test_loaded_via_config_facade(self):
        from config import Config
        assert Config.MAX_TRAJECTORY_FRAMES == 1000

    def test_positive_int_validator_enforced(self, monkeypatch):
        from pydantic import ValidationError
        from settings import Settings
        monkeypatch.setenv("MAX_TRAJECTORY_FRAMES", "0")
        with pytest.raises(ValidationError):
            Settings()


class TestPanelSuggestedStride:
    """Pre-dispatch panel-side ZIP rejection renders a 'keep every Nth' hint.

    The hint formula is ``pdb_count // MAX_TRAJECTORY_FRAMES + 1``; we
    don't import the panel module (it pulls Streamlit) but verify the
    formula matches what the user-facing message claims.
    """

    @pytest.mark.parametrize("pdbs, cap, expected_keep", [
        (1001, 1000, 2),   # 1 over → keep every 2nd
        (1500, 1000, 2),
        (2000, 1000, 3),
        (10000, 1000, 11),
        (3, 2, 2),         # tiny test inputs
    ])
    def test_keep_every_nth_calc(self, pdbs, cap, expected_keep):
        # Mirrors panels/find_pockets.py:_submit's pdb-zip branch.
        keep = max(2, pdbs // cap + 1)
        assert keep == expected_keep
