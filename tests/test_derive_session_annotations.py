"""Tests for ``derive_session_annotations`` — the DB-driven annotation builder
the viewer fragment calls on every poll.

The helper reads two artefacts off disk for each job kind:

* find_pockets / pipeline → ``<results_dir>/<legacy>/pockets/pockets.csv``
* cluster   / pipeline    → ``<results_dir>/<legacy>/pocket_clusters/{cluster_representatives.csv,pockets_clustered.csv}``

These tests synthesise both layouts in a ``tmp_path`` directory, write
Job rows via ``db.jobs.create_for_legacy`` + status-update them to
"completed", and confirm the helper returns the expected annotation
sections.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest


def _write_pockets_csv(results_dir: Path, legacy_id: str) -> None:
    pockets_dir = results_dir / legacy_id / "pockets"
    pockets_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
        [
            {"probability": 0.9, "residues": "A_125 A_126 A_127", "pocket_index": 0,
             "File name": "frame_0000.pdb"},
            {"probability": 0.7, "residues": "B_10 B_11 B_12 B_13", "pocket_index": 1,
             "File name": "frame_0000.pdb"},
        ]
    )
    df.to_csv(pockets_dir / "pockets.csv", index=False)


def _write_cluster_csvs(results_dir: Path, legacy_id: str) -> None:
    cluster_dir = results_dir / legacy_id / "pocket_clusters"
    cluster_dir.mkdir(parents=True, exist_ok=True)
    reps = pd.DataFrame(
        [
            {"cluster": 0, "residues": "A_125 A_126 A_127", "probability": 0.9},
            {"cluster": 1, "residues": "B_10 B_11 B_12", "probability": 0.6},
        ]
    )
    reps.to_csv(cluster_dir / "cluster_representatives.csv", index=False)

    clustered = pd.DataFrame(
        [
            {"cluster": 0, "residues": "A_125 A_126 A_127"},
            {"cluster": 0, "residues": "A_125 A_128"},
            {"cluster": 1, "residues": "B_10 B_11 B_12"},
        ]
    )
    clustered.to_csv(cluster_dir / "pockets_clustered.csv", index=False)


def test_derive_returns_empty_when_no_jobs(tmp_path, db_with_schema):
    from components.molstar_annotations import derive_session_annotations
    from db.sessions import create_session

    s = create_session()
    out = derive_session_annotations(s.id, tmp_path)
    assert out == {}


def test_derive_returns_empty_when_session_id_is_none(tmp_path):
    from components.molstar_annotations import derive_session_annotations

    assert derive_session_annotations(None, tmp_path) == {}


def test_derive_returns_pockets_from_latest_find_pockets(tmp_path, db_with_schema):
    from components.molstar_annotations import derive_session_annotations
    from db.jobs import create_for_legacy, update_by_legacy_id
    from db.sessions import create_session

    s = create_session()
    legacy = "find_pockets_test_001"
    create_for_legacy(s.id, "find_pockets", legacy)
    update_by_legacy_id(legacy, "completed")
    _write_pockets_csv(tmp_path, legacy)

    out = derive_session_annotations(s.id, tmp_path)
    assert "pockets" in out
    pockets = out["pockets"]
    assert len(pockets) == 2
    # The highest-probability pocket sorts first.
    assert pockets[0]["residues"] == ["A_125", "A_126", "A_127"]


def test_derive_returns_clusters_from_latest_cluster_job(tmp_path, db_with_schema):
    from components.molstar_annotations import derive_session_annotations
    from db.jobs import create_for_legacy, update_by_legacy_id
    from db.sessions import create_session

    s = create_session()
    legacy = "cluster_test_001"
    create_for_legacy(s.id, "cluster", legacy)
    update_by_legacy_id(legacy, "completed")
    _write_cluster_csvs(tmp_path, legacy)

    out = derive_session_annotations(s.id, tmp_path)
    assert "clusters" in out
    clusters = out["clusters"]
    assert len(clusters) == 2
    cluster_0 = next(c for c in clusters if c["cluster_id"] == 0)
    assert cluster_0["residues"] == ["A_125", "A_126", "A_127", "A_128"]


def test_derive_handles_missing_csv_gracefully(tmp_path, db_with_schema):
    """Job row says completed but no CSV on disk → helper omits the section."""
    from components.molstar_annotations import derive_session_annotations
    from db.jobs import create_for_legacy, update_by_legacy_id
    from db.sessions import create_session

    s = create_session()
    create_for_legacy(s.id, "find_pockets", "ghost_job_no_csv")
    update_by_legacy_id("ghost_job_no_csv", "completed")

    out = derive_session_annotations(s.id, tmp_path)
    # No pockets section because the CSV doesn't exist.
    assert "pockets" not in out
