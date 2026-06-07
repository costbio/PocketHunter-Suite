"""Tests for the v2 Phase B B2 viewer-trajectory pipeline.

Builds a tiny synthetic structure in memory + writes a handful of single-
model PDB "frames" to a tmpdir, then verifies that the combined multi-
model mmCIF parses back with the expected model count.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from task_errors import ViewerConversionError


def _write_synthetic_pdb_frames(tmpdir: Path, n_frames: int = 3) -> Path:
    """Write ``n_frames`` minimal one-model PDBs into ``tmpdir/pdbs/``.

    Each frame contains the same 4-atom skeleton (one residue, four atoms)
    so the topology stays consistent across frames — the requirement gemmi
    needs to assemble them into a multi-model structure.
    """
    pdbs_dir = tmpdir / "pdbs"
    pdbs_dir.mkdir(parents=True, exist_ok=True)

    # PDB ATOM records: simple 4-atom residue (CA + 3 sidechain) of chain A,
    # residue 1, ALA. Coordinates vary per frame so models differ.
    template = (
        "CRYST1   50.000   50.000   50.000  90.00  90.00  90.00 P 1\n"
        "ATOM      1  N   ALA A   1    {0:8.3f}{1:8.3f}{2:8.3f}  1.00  0.00           N\n"
        "ATOM      2  CA  ALA A   1    {3:8.3f}{4:8.3f}{5:8.3f}  1.00  0.00           C\n"
        "ATOM      3  C   ALA A   1    {6:8.3f}{7:8.3f}{8:8.3f}  1.00  0.00           C\n"
        "ATOM      4  O   ALA A   1    {9:8.3f}{10:8.3f}{11:8.3f}  1.00  0.00           O\n"
        "TER       5      ALA A   1\n"
        "END\n"
    )
    for i in range(n_frames):
        offset = i * 0.1  # nudge coords per frame
        coords = [
            0.0 + offset, 0.0, 0.0,
            1.5 + offset, 0.0, 0.0,
            2.5 + offset, 1.0, 0.0,
            3.0 + offset, 1.5, 1.0,
        ]
        (pdbs_dir / f"frame_{i:04d}.pdb").write_text(template.format(*coords))
    return pdbs_dir


def test_convert_produces_nonempty_pdb(tmp_path):
    from viewer_pipeline import convert_pdb_dir_to_viewer

    pdbs_dir = _write_synthetic_pdb_frames(tmp_path, n_frames=3)
    out_path = tmp_path / "viewer.pdb"
    result = convert_pdb_dir_to_viewer(pdbs_dir, out_path)
    # Returns the full build manifest now (B12 stride).
    assert Path(result["viewer_file_path"]) == out_path
    assert result["n_viewer_models"] == 3
    assert result["n_extracted_frames"] == 3
    assert result["viewer_stride"] == 1
    assert out_path.exists()
    assert out_path.stat().st_size > 0


def test_stride_applied_when_count_exceeds_cap(tmp_path, monkeypatch):
    """Stride decimates the input set when n_extracted > MAX_VIEWER_LOADED_FRAMES."""
    import viewer_pipeline
    from viewer_pipeline import convert_pdb_dir_to_viewer

    monkeypatch.setattr(viewer_pipeline, "MAX_VIEWER_LOADED_FRAMES", 4)
    pdbs_dir = _write_synthetic_pdb_frames(tmp_path, n_frames=12)
    out_path = tmp_path / "viewer.pdb"

    info = convert_pdb_dir_to_viewer(pdbs_dir, out_path)
    # ceil(12/4) = 3 → [::3] picks 4 of 12.
    assert info["viewer_stride"] == 3
    assert info["n_viewer_models"] == 4
    assert info["n_extracted_frames"] == 12


def test_manifest_logical_to_viewer_map_is_correct(tmp_path, monkeypatch):
    """The manifest's logical_to_viewer map names every kept frame at its index."""
    import json
    import viewer_pipeline
    from viewer_pipeline import convert_pdb_dir_to_viewer, sorted_frame_pdbs

    monkeypatch.setattr(viewer_pipeline, "MAX_VIEWER_LOADED_FRAMES", 3)
    pdbs_dir = _write_synthetic_pdb_frames(tmp_path, n_frames=9)
    out_path = tmp_path / "viewer.pdb"
    convert_pdb_dir_to_viewer(pdbs_dir, out_path)

    manifest = json.loads((tmp_path / "viewer_index.json").read_text())
    all_sorted = [p.name for p in sorted_frame_pdbs(pdbs_dir)]
    expected_selected = all_sorted[::3]  # ceil(9/3) = 3 → [::3] picks 3 of 9
    assert manifest["stride"] == 3
    assert manifest["n_extracted_frames"] == 9
    assert manifest["n_viewer_models"] == 3
    assert list(manifest["logical_to_viewer"].keys()) == expected_selected
    assert list(manifest["logical_to_viewer"].values()) == [1, 2, 3]


def test_manifest_written_with_stride_one_for_small_input(tmp_path):
    """Stride-1 path (n <= cap) still writes a manifest with every frame mapped."""
    import json
    from viewer_pipeline import convert_pdb_dir_to_viewer

    pdbs_dir = _write_synthetic_pdb_frames(tmp_path, n_frames=3)
    out_path = tmp_path / "viewer.pdb"
    convert_pdb_dir_to_viewer(pdbs_dir, out_path)

    manifest = json.loads((tmp_path / "viewer_index.json").read_text())
    assert manifest["stride"] == 1
    assert manifest["n_viewer_models"] == 3
    assert len(manifest["logical_to_viewer"]) == 3
    # Every logical filename maps to a dense 1-based index.
    assert set(manifest["logical_to_viewer"].values()) == {1, 2, 3}


def test_link_viewer_for_session_mirrors_manifest_and_frames(tmp_path, monkeypatch):
    """link_viewer_for_session copies manifest + per-frame PDBs into static/."""
    import viewer_pipeline
    from viewer_pipeline import convert_pdb_dir_to_viewer, link_viewer_for_session

    monkeypatch.setattr(viewer_pipeline, "MAX_VIEWER_LOADED_FRAMES", 2)
    pdbs_dir = _write_synthetic_pdb_frames(tmp_path, n_frames=5)
    job_dir = tmp_path / "job_dir"
    job_dir.mkdir()
    # link_viewer_for_session globs the viewer file's parent for pdbs/ —
    # place the per-frame inputs there so the symlink loop finds them.
    target_pdbs = job_dir / "pdbs"
    target_pdbs.mkdir()
    for p in pdbs_dir.glob("*.pdb"):
        (target_pdbs / p.name).write_text(p.read_text())

    out_path = job_dir / "viewer.pdb"
    convert_pdb_dir_to_viewer(target_pdbs, out_path)

    base = tmp_path / "repo"
    base.mkdir()
    url = link_viewer_for_session("short_abc", out_path, base)
    assert url == "/app/static/short_abc/viewer.pdb"

    static_root = base / "static" / "short_abc"
    assert (static_root / "viewer.pdb").exists()
    assert (static_root / "viewer_index.json").exists()
    frames_root = static_root / "frames"
    assert frames_root.is_dir()
    # All 5 per-frame PDBs should be mirrored (including the 3 that
    # aren't baked into the strided viewer.pdb).
    assert sorted(p.name for p in frames_root.glob("*.pdb")) == sorted(
        p.name for p in target_pdbs.glob("*.pdb")
    )


def test_pdb_is_multi_model_with_expected_count(tmp_path):
    """Round-trip: the file we wrote contains exactly 3 MODEL blocks."""
    import gemmi

    from viewer_pipeline import convert_pdb_dir_to_viewer

    pdbs_dir = _write_synthetic_pdb_frames(tmp_path, n_frames=3)
    out_path = tmp_path / "viewer.pdb"
    convert_pdb_dir_to_viewer(pdbs_dir, out_path)

    structure = gemmi.read_structure(str(out_path))
    assert len(structure) == 3, f"expected 3 models, got {len(structure)}"
    # Multi-model PDB grammar — header / MODEL / ENDMDL bracketing.
    text = out_path.read_text()
    assert text.count("\nMODEL ") + text.startswith("MODEL ") >= 3
    assert text.count("\nENDMDL") >= 3


def test_empty_directory_raises_viewer_conversion_error(tmp_path):
    from viewer_pipeline import convert_pdb_dir_to_viewer

    empty = tmp_path / "no_pdbs"
    empty.mkdir()
    with pytest.raises(ViewerConversionError, match="No .pdb files"):
        convert_pdb_dir_to_viewer(empty, tmp_path / "out.pdb")


def test_estimate_viewer_size_sums_input_files(tmp_path):
    from viewer_pipeline import estimate_viewer_size

    pdbs_dir = _write_synthetic_pdb_frames(tmp_path, n_frames=3)
    est = estimate_viewer_size(pdbs_dir)
    actual = sum(p.stat().st_size for p in pdbs_dir.glob("*.pdb"))
    assert est == actual


def test_estimate_returns_zero_for_empty_dir(tmp_path):
    from viewer_pipeline import estimate_viewer_size

    empty = tmp_path / "empty"
    empty.mkdir()
    assert estimate_viewer_size(empty) == 0


def test_max_viewer_bytes_sourced_from_settings():
    """MAX_VIEWER_BYTES is now env-configurable via settings (was a
    hardcoded constant). Tracks the live settings value at module-load."""
    import viewer_pipeline
    from settings import settings

    assert viewer_pipeline.MAX_VIEWER_BYTES == settings.MAX_VIEWER_BYTES


def test_max_viewer_bytes_default_is_1gb():
    """Default kept at 1 GB for backwards-compat with the previous
    hardcoded value."""
    from settings import Settings

    fresh = Settings(_env_file=None)  # avoid picking up local .env overrides
    assert fresh.MAX_VIEWER_BYTES == 1024 * 1024 * 1024


def test_tasks_write_viewer_file_size_cap_writes_warning(tmp_path, monkeypatch):
    """When the size estimate exceeds MAX_VIEWER_BYTES, the helper returns a
    warning dict + skips conversion. No exception."""
    import viewer_pipeline
    import tasks

    pdbs_dir = _write_synthetic_pdb_frames(tmp_path, n_frames=3)
    # Force RESULTS_DIR to a tmpdir so we can intercept the would-be output.
    job_id = "test_size_cap_job"
    monkeypatch.setattr(tasks, "RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(viewer_pipeline, "MAX_VIEWER_BYTES", 0)

    info = tasks._write_viewer_file(job_id, str(pdbs_dir))
    assert "viewer_file_warning" in info
    assert "viewer_file_path" not in info
    assert not (tmp_path / "results" / job_id / "viewer.pdb").exists()


def test_tasks_write_viewer_file_success_returns_path(tmp_path, monkeypatch):
    """Normal path: tasks._write_viewer_file returns path + format."""
    import tasks

    pdbs_dir = _write_synthetic_pdb_frames(tmp_path, n_frames=3)
    job_id = "test_success_job"
    monkeypatch.setattr(tasks, "RESULTS_DIR", str(tmp_path / "results"))

    info = tasks._write_viewer_file(job_id, str(pdbs_dir))
    assert info["viewer_file_format"] == "pdb"
    out_path = Path(info["viewer_file_path"])
    assert out_path.exists()
    assert out_path.stat().st_size > 0
    # Manifest fields surface in result_info so the Job row carries them.
    assert info["n_viewer_models"] == 3
    assert info["n_extracted_frames"] == 3
    assert info["viewer_stride"] == 1
    assert Path(info["viewer_index_path"]).exists()


def test_tasks_write_viewer_file_handles_failure(tmp_path, monkeypatch):
    """When conversion raises, tasks._write_viewer_file returns
    viewer_file_error rather than propagating."""
    import tasks

    # Point at an empty directory — convert_pdb_dir_to_viewer raises
    # ViewerConversionError, which the wrapper catches.
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    job_id = "test_failure_job"
    monkeypatch.setattr(tasks, "RESULTS_DIR", str(tmp_path / "results"))

    info = tasks._write_viewer_file(job_id, str(empty_dir))
    assert "viewer_file_error" in info
    assert "No .pdb files" in info["viewer_file_error"]
    assert "viewer_file_path" not in info
