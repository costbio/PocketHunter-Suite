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


def test_convert_produces_nonempty_mmcif(tmp_path):
    from viewer_pipeline import convert_pdb_dir_to_viewer

    pdbs_dir = _write_synthetic_pdb_frames(tmp_path, n_frames=3)
    out_path = tmp_path / "viewer.cif"
    result = convert_pdb_dir_to_viewer(pdbs_dir, out_path)
    assert result == out_path
    assert out_path.exists()
    assert out_path.stat().st_size > 0


def test_mmcif_is_gemmi_parseable_with_expected_model_count(tmp_path):
    """Round-trip: the file we wrote contains exactly 3 models."""
    import gemmi

    from viewer_pipeline import convert_pdb_dir_to_viewer

    pdbs_dir = _write_synthetic_pdb_frames(tmp_path, n_frames=3)
    out_path = tmp_path / "viewer.cif"
    convert_pdb_dir_to_viewer(pdbs_dir, out_path)

    structure = gemmi.read_structure(str(out_path))
    assert len(structure) == 3, f"expected 3 models, got {len(structure)}"


def test_empty_directory_raises_viewer_conversion_error(tmp_path):
    from viewer_pipeline import convert_pdb_dir_to_viewer

    empty = tmp_path / "no_pdbs"
    empty.mkdir()
    with pytest.raises(ViewerConversionError, match="No .pdb files"):
        convert_pdb_dir_to_viewer(empty, tmp_path / "out.cif")


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
    assert not (tmp_path / "results" / job_id / "viewer.cif").exists()


def test_tasks_write_viewer_file_success_returns_path(tmp_path, monkeypatch):
    """Normal path: tasks._write_viewer_file returns path + format."""
    import tasks

    pdbs_dir = _write_synthetic_pdb_frames(tmp_path, n_frames=3)
    job_id = "test_success_job"
    monkeypatch.setattr(tasks, "RESULTS_DIR", str(tmp_path / "results"))

    info = tasks._write_viewer_file(job_id, str(pdbs_dir))
    assert info["viewer_file_format"] == "mmcif"
    out_path = Path(info["viewer_file_path"])
    assert out_path.exists()
    assert out_path.stat().st_size > 0


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
