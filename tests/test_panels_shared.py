"""Tests for ``panels._shared.latest_job_of_kind``.

The helper drives the "latest result for this stage" lookups every panel
makes against the session's Job rows. Three cases worth pinning down:

* empty session → ``None``;
* mixed-kind rows → only matching kinds considered;
* multiple matching rows → most-recently-updated wins.

Panel render functions themselves are UI-heavy and verified by manual
browser passes; no Streamlit-side unit tests live here.
"""
from __future__ import annotations

import time

import pytest


def test_latest_job_of_kind_returns_none_when_no_jobs(db_with_schema):
    from db.sessions import create_session
    from panels._shared import latest_job_of_kind

    s = create_session()
    assert latest_job_of_kind(s.id, ("find_pockets", "pipeline")) is None


def test_latest_job_of_kind_filters_by_kind(db_with_schema):
    from db.jobs import create_for_legacy
    from db.sessions import create_session
    from panels._shared import latest_job_of_kind

    s = create_session()
    create_for_legacy(s.id, "cluster", "cluster_a")
    create_for_legacy(s.id, "docking", "docking_b")
    create_for_legacy(s.id, "find_pockets", "find_pockets_c")

    result = latest_job_of_kind(s.id, ("find_pockets", "pipeline"))
    assert result is not None
    assert result.get("legacy_id") == "find_pockets_c"
    assert result.get("kind") == "find_pockets"


def test_latest_job_of_kind_returns_newest_when_multiple(db_with_schema):
    from db.jobs import create_for_legacy, update_by_legacy_id
    from db.sessions import create_session
    from panels._shared import latest_job_of_kind

    s = create_session()
    create_for_legacy(s.id, "find_pockets", "find_pockets_first")
    # Briefly sleep so the second row's created_at + updated_at exceed
    # the first's by enough for the ISO string comparison to be definitive.
    time.sleep(0.01)
    create_for_legacy(s.id, "find_pockets", "find_pockets_second")

    # Bump the second row's updated_at by writing a status update.
    time.sleep(0.01)
    update_by_legacy_id("find_pockets_second", "completed")

    result = latest_job_of_kind(s.id, ("find_pockets",))
    assert result is not None
    assert result.get("legacy_id") == "find_pockets_second"


def test_latest_job_of_kind_with_none_session_returns_none():
    """Pure-function guard: no DB lookup attempted when session_id is None."""
    from panels._shared import latest_job_of_kind

    assert latest_job_of_kind(None, ("find_pockets",)) is None


# ── viewer_target_for_filename (B12 stride) ────────────────────────────


def _write_job_with_manifest(
    results_dir,
    job_id: str,
    n_frames: int,
    stride: int,
) -> None:
    """Synthesize a results/<job>/pdbs/ tree + a stride manifest."""
    import json
    from pathlib import Path

    job_dir = Path(results_dir) / job_id
    pdbs_dir = job_dir / "pdbs"
    pdbs_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for i in range(1, n_frames + 1):
        name = f"traj_fit_{i}.pdb"
        (pdbs_dir / name).write_text(f"# fake frame {i}\n")
        names.append(name)
    # Replicate viewer_pipeline's sort: numerical by trailing index.
    names.sort(
        key=lambda n: int(n.rsplit("_", 1)[1].removesuffix(".pdb"))
    )
    selected = names[::stride] if stride > 1 else names
    manifest = {
        "stride": stride,
        "n_extracted_frames": n_frames,
        "n_viewer_models": len(selected),
        "logical_to_viewer": {n: i for i, n in enumerate(selected, start=1)},
    }
    (job_dir / "viewer_index.json").write_text(json.dumps(manifest))


def test_viewer_target_for_in_strided_filename_has_model_index(tmp_path, monkeypatch):
    """A frame in the strided set resolves to a non-None viewer_model."""
    import panels._shared as ps
    from config import Config

    monkeypatch.setattr(Config, "RESULTS_DIR", tmp_path)
    _write_job_with_manifest(tmp_path, "job_x", n_frames=9, stride=3)
    # Clear the streamlit cache shim — _frame_index_map and
    # _viewer_manifest are cache_data, which decorates to the underlying
    # function in test-without-streamlit setups.
    ps._frame_index_map.clear() if hasattr(ps._frame_index_map, "clear") else None
    ps._viewer_manifest.clear() if hasattr(ps._viewer_manifest, "clear") else None

    target = ps.viewer_target_for_filename("job_x", "traj_fit_1.pdb")
    assert target is not None
    assert target.filename == "traj_fit_1.pdb"
    assert target.logical_index == 1
    assert target.viewer_model == 1  # First selected frame at stride=3.


def test_viewer_target_for_out_of_strided_filename_has_none_model(tmp_path, monkeypatch):
    """A frame outside the strided set resolves to viewer_model=None."""
    import panels._shared as ps
    from config import Config

    monkeypatch.setattr(Config, "RESULTS_DIR", tmp_path)
    _write_job_with_manifest(tmp_path, "job_y", n_frames=9, stride=3)
    ps._frame_index_map.clear() if hasattr(ps._frame_index_map, "clear") else None
    ps._viewer_manifest.clear() if hasattr(ps._viewer_manifest, "clear") else None

    # Frame 2 is skipped at stride=3 (kept: 1, 4, 7).
    target = ps.viewer_target_for_filename("job_y", "traj_fit_2.pdb")
    assert target is not None
    assert target.filename == "traj_fit_2.pdb"
    assert target.logical_index == 2
    assert target.viewer_model is None


def test_viewer_target_normalises_p2rank_predictions_suffix(tmp_path, monkeypatch):
    """The ``_predictions`` suffix p2rank appends in pockets.csv is stripped."""
    import panels._shared as ps
    from config import Config

    monkeypatch.setattr(Config, "RESULTS_DIR", tmp_path)
    _write_job_with_manifest(tmp_path, "job_z", n_frames=3, stride=1)
    ps._frame_index_map.clear() if hasattr(ps._frame_index_map, "clear") else None
    ps._viewer_manifest.clear() if hasattr(ps._viewer_manifest, "clear") else None

    target = ps.viewer_target_for_filename("job_z", "traj_fit_1.pdb_predictions")
    assert target is not None
    assert target.filename == "traj_fit_1.pdb"
    assert target.viewer_model == 1


def test_viewer_target_returns_none_for_unknown_filename(tmp_path, monkeypatch):
    import panels._shared as ps
    from config import Config

    monkeypatch.setattr(Config, "RESULTS_DIR", tmp_path)
    _write_job_with_manifest(tmp_path, "job_q", n_frames=3, stride=1)
    ps._frame_index_map.clear() if hasattr(ps._frame_index_map, "clear") else None
    ps._viewer_manifest.clear() if hasattr(ps._viewer_manifest, "clear") else None

    assert ps.viewer_target_for_filename("job_q", "no_such_frame.pdb") is None


def test_viewer_target_falls_back_to_dense_map_without_manifest(tmp_path, monkeypatch):
    """Legacy job (no manifest): treat as stride=1 — viewer_model == logical."""
    import panels._shared as ps
    from config import Config
    from pathlib import Path

    monkeypatch.setattr(Config, "RESULTS_DIR", tmp_path)
    legacy_dir = tmp_path / "legacy_job" / "pdbs"
    legacy_dir.mkdir(parents=True)
    for i in (1, 2, 3):
        (legacy_dir / f"traj_fit_{i}.pdb").write_text("# legacy\n")
    # No viewer_index.json written.
    ps._frame_index_map.clear() if hasattr(ps._frame_index_map, "clear") else None
    ps._viewer_manifest.clear() if hasattr(ps._viewer_manifest, "clear") else None

    target = ps.viewer_target_for_filename("legacy_job", "traj_fit_2.pdb")
    assert target is not None
    assert target.viewer_model == target.logical_index


def test_viewer_target_returns_none_for_blank_inputs():
    from panels._shared import viewer_target_for_filename

    assert viewer_target_for_filename("", "anything.pdb") is None
    assert viewer_target_for_filename("job", "") is None
    assert viewer_target_for_filename("job", None) is None
