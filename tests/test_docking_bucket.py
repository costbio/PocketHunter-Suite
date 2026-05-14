"""Tests for the docking-selection bucket helper (B11.2)."""
from __future__ import annotations

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _fake_session_state(monkeypatch):
    """Replace ``st.session_state`` with a plain dict for these tests.

    The bucket helper imports streamlit lazily inside ``_state``; we
    swap out ``st.session_state`` with a dict so we don't need a real
    Streamlit ScriptRunContext.
    """
    import streamlit as st

    fake: dict = {}
    monkeypatch.setattr(st, "session_state", fake)
    yield fake


def _sample_pocket(fpi="84_1", source="find_pockets_x", **overrides) -> dict:
    base = {
        "source_job_id": source,
        "File name": f"traj_{fpi}.pdb_predictions",
        "Frame_pocket_index": fpi,
        "Frame": int(fpi.split("_")[0]),
        "pocket_index": int(fpi.split("_")[1]),
        "probability": 0.5,
        "residues": "A_10 A_11 A_12",
        "cluster": None,
    }
    base.update(overrides)
    return base


class TestBucketCRUD:
    def test_empty_bucket_returns_empty_list(self):
        from panels._docking_bucket import current, count

        assert current() == []
        assert count() == 0

    def test_add_returns_count(self):
        from panels._docking_bucket import add, count

        n = add([_sample_pocket("84_1"), _sample_pocket("85_2")])
        assert n == 2
        assert count() == 2

    def test_add_dedups_by_composite_key(self):
        from panels._docking_bucket import add, count

        add([_sample_pocket("84_1"), _sample_pocket("85_2")])
        # Adding the same pockets again returns 0 added.
        n = add([_sample_pocket("84_1"), _sample_pocket("85_2")])
        assert n == 0
        assert count() == 2

    def test_add_distinguishes_by_source_job(self):
        from panels._docking_bucket import add, count

        add([_sample_pocket("84_1", source="job_a")])
        n = add([_sample_pocket("84_1", source="job_b")])
        # Same Frame_pocket_index but different source → both kept.
        assert n == 1
        assert count() == 2

    def test_remove_by_composite_key(self):
        from panels._docking_bucket import add, remove, count

        add([_sample_pocket("84_1"), _sample_pocket("85_2"), _sample_pocket("86_3")])
        n = remove([("find_pockets_x", "85_2")])
        assert n == 1
        assert count() == 2

    def test_clear(self):
        from panels._docking_bucket import add, clear, count

        add([_sample_pocket("84_1"), _sample_pocket("85_2")])
        clear()
        assert count() == 0


class TestCsvRoundTrip:
    def test_to_csv_writes_required_columns(self, tmp_path):
        from panels._docking_bucket import add, to_csv

        add([_sample_pocket("84_1", probability=0.85),
             _sample_pocket("85_2", probability=0.6)])
        out = to_csv(tmp_path / "bucket.csv")
        df = pd.read_csv(out)
        # docking task requires File name + residues.
        assert "File name" in df.columns
        assert "residues" in df.columns
        assert len(df) == 2

    def test_to_csv_raises_on_empty(self, tmp_path):
        from panels._docking_bucket import to_csv

        with pytest.raises(ValueError, match="empty"):
            to_csv(tmp_path / "empty.csv")


class TestFromPocketsDf:
    def test_converts_rows_with_full_columns(self):
        from panels._docking_bucket import from_pockets_df

        df = pd.DataFrame([
            {"File name": "f1.pdb_predictions", "Frame_pocket_index": "84_1",
             "Frame": 84, "pocket_index": 1, "probability": 0.9,
             "residues": "A_10 A_11", "cluster": 0},
            {"File name": "f2.pdb_predictions", "Frame_pocket_index": "85_2",
             "Frame": 85, "pocket_index": 2, "probability": 0.6,
             "residues": "A_20 A_21", "cluster": 1},
        ])
        entries = from_pockets_df(df, source_job_id="job_x")
        assert len(entries) == 2
        assert entries[0]["source_job_id"] == "job_x"
        assert entries[0]["Frame_pocket_index"] == "84_1"
        assert entries[0]["cluster"] == 0
        assert entries[1]["cluster"] == 1

    def test_cluster_override(self):
        from panels._docking_bucket import from_pockets_df

        df = pd.DataFrame([
            {"File name": "f1.pdb", "Frame_pocket_index": "1_0",
             "Frame": 1, "pocket_index": 0, "probability": 0.5,
             "residues": "A_10", "cluster": 99},
        ])
        entries = from_pockets_df(df, source_job_id="x", cluster_override=7)
        assert entries[0]["cluster"] == 7  # override wins
