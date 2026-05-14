"""Tests for ``components.molstar_annotations`` builders.

The Python builders are pure — they shape DataFrame rows + SDF data into
the annotation dict the JS side of ``molstar_viewer`` consumes. JS-side
rendering is verified by manual browser passes (no headless WebGL).
"""
from __future__ import annotations

import pandas as pd
import pytest


def test_pocket_annotations_keeps_top_n_in_probability_order():
    from components.molstar_annotations import pocket_annotations_from_df

    df = pd.DataFrame(
        [
            {"probability": p, "residues": "A_1 A_2 A_3 A_4", "pocket_index": i}
            for i, p in enumerate([0.10, 0.90, 0.85, 0.40, 0.50, 0.30, 0.20, 0.00, 0.95, 0.70])
        ]
    )
    out = pocket_annotations_from_df(df, top_n=5)
    assert len(out) == 5
    # Labels should carry the highest-probability pocket_indexes first.
    # We don't depend on label format — just that probabilities are descending.
    # Reconstruct the probabilities from the order in which rows were kept:
    kept_probs = []
    for entry in out:
        # The label contains "p=<float>".
        for token in entry["label"].split(" · "):
            if token.startswith("p="):
                kept_probs.append(float(token[2:]))
                break
    assert kept_probs == sorted(kept_probs, reverse=True)
    assert kept_probs[0] == pytest.approx(0.95)


def test_pocket_annotations_skips_tiny_pockets():
    from components.molstar_annotations import pocket_annotations_from_df

    df = pd.DataFrame(
        [
            {"probability": 0.9, "residues": "A_1 A_2", "pocket_index": 0},        # 2 → skip
            {"probability": 0.8, "residues": "A_3 A_4 A_5", "pocket_index": 1},    # 3 → keep
            {"probability": 0.7, "residues": "A_6", "pocket_index": 2},            # 1 → skip
        ]
    )
    out = pocket_annotations_from_df(df, top_n=10)
    assert len(out) == 1
    assert out[0]["residues"] == ["A_3", "A_4", "A_5"]


def test_pocket_annotations_parses_residues_column():
    from components.molstar_annotations import pocket_annotations_from_df

    df = pd.DataFrame(
        [{"probability": 0.5, "residues": "A_125 A_147 B_203 A_212", "pocket_index": 0}]
    )
    out = pocket_annotations_from_df(df, top_n=5)
    assert len(out) == 1
    assert out[0]["residues"] == ["A_125", "A_147", "B_203", "A_212"]
    assert out[0]["color"].startswith("#")


def test_cluster_annotations_distinct_colors():
    from components.molstar_annotations import cluster_annotations_from_df

    df_reps = pd.DataFrame(
        [
            {"cluster": 0, "residues": "A_1 A_2 A_3"},
            {"cluster": 1, "residues": "B_10 B_11 B_12"},
            {"cluster": 2, "residues": "C_20 C_21 C_22"},
        ]
    )
    out = cluster_annotations_from_df(None, df_reps)
    assert len(out) == 3
    colors = {entry["color"] for entry in out}
    assert len(colors) == 3, f"expected 3 distinct colors, got {colors}"
    assert all(c.startswith("#") for c in colors)


def test_cluster_annotations_unions_per_cluster_residues():
    from components.molstar_annotations import cluster_annotations_from_df

    df_clustered = pd.DataFrame(
        [
            {"cluster": 0, "residues": "A_1 A_2"},
            {"cluster": 0, "residues": "A_2 A_3"},
            {"cluster": 1, "residues": "B_10 B_11"},
        ]
    )
    df_reps = pd.DataFrame(
        [
            {"cluster": 0, "residues": "A_1"},
            {"cluster": 1, "residues": "B_10"},
        ]
    )
    out = cluster_annotations_from_df(df_clustered, df_reps)
    cluster_0 = next(e for e in out if e["cluster_id"] == 0)
    assert cluster_0["residues"] == ["A_1", "A_2", "A_3"]


def test_ligand_pose_annotation_returns_none_when_sdf_missing():
    from components.molstar_annotations import ligand_pose_annotation_from_pose

    # No output_sdf at all.
    assert ligand_pose_annotation_from_pose({"mode": 1}) is None
    # No mode.
    assert ligand_pose_annotation_from_pose({"output_sdf": "/tmp/nope.sdf"}) is None
    # Empty pose.
    assert ligand_pose_annotation_from_pose({}) is None
    assert ligand_pose_annotation_from_pose(None) is None


def test_merge_annotations_replaces_section():
    from components.molstar_annotations import merge_annotations

    base = {"pockets": [{"label": "old"}], "clusters": [{"cluster_id": 0}]}
    merge_annotations(base, pockets=[{"label": "new"}])
    assert base["pockets"] == [{"label": "new"}]
    assert base["clusters"] == [{"cluster_id": 0}]  # unchanged


def test_merge_annotations_none_clears_section():
    from components.molstar_annotations import merge_annotations

    base = {"pockets": [{"label": "x"}], "ligand_pose": {"sdf": "abc"}}
    merge_annotations(base, ligand_pose=None)
    assert "ligand_pose" not in base
    assert base["pockets"] == [{"label": "x"}]
