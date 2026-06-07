"""Unit tests for the shared download helpers.

The module is dependency-free (just stdlib + Path) so these run
directly without spinning Streamlit. Covers:

- ZIP-builder contracts (file inclusion, metadata.csv embedding, arcnames)
- Size-cap behavior (smallest-first add, omit on overflow, ordering)
- current_view_artifact branch matrix (with/without ligand pose)
- Filename + MIME guarantees
"""
from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

import pytest


def _write_pdb(dir_: Path, name: str, size_bytes: int = 200) -> Path:
    """Write a minimal PDB-like file of approximately ``size_bytes``."""
    p = dir_ / name
    p.write_text("X" * size_bytes)
    return p


# ── build_pocket_zip ────────────────────────────────────────────────


class TestBuildPocketZip:
    def test_zip_contains_all_pdbs_and_metadata(self, tmp_path):
        from downloads import build_pocket_zip

        a = _write_pdb(tmp_path, "frame_1.pdb", size_bytes=100)
        b = _write_pdb(tmp_path, "frame_2.pdb", size_bytes=200)
        rows = [
            {"filename": "frame_1.pdb", "frame": 1, "probability": 0.81},
            {"filename": "frame_2.pdb", "frame": 2, "probability": 0.65},
        ]
        zip_bytes, omitted = build_pocket_zip(
            [(a, "frame_1.pdb"), (b, "frame_2.pdb")],
            rows,
            cap_bytes=10_000,
        )
        assert omitted == []

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            assert "frame_1.pdb" in names
            assert "frame_2.pdb" in names
            assert "metadata.csv" in names
            meta = zf.read("metadata.csv").decode("utf-8").splitlines()
            assert meta[0] == "filename,frame,probability"
            assert "frame_1.pdb,1,0.81" in meta
            assert "frame_2.pdb,2,0.65" in meta

    def test_cap_omits_smallest_first_strategy(self, tmp_path):
        """Smallest-first add maximises file count under the cap."""
        from downloads import build_pocket_zip

        # 100, 200, 5000 bytes. Cap=400 lets the two small ones in
        # but blocks the 5000-byte file.
        a = _write_pdb(tmp_path, "small.pdb", size_bytes=100)
        b = _write_pdb(tmp_path, "medium.pdb", size_bytes=200)
        c = _write_pdb(tmp_path, "big.pdb", size_bytes=5000)
        zip_bytes, omitted = build_pocket_zip(
            [(c, "big.pdb"), (b, "medium.pdb"), (a, "small.pdb")],
            [],
            cap_bytes=400,
        )
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            assert "small.pdb" in names
            assert "medium.pdb" in names
            assert "big.pdb" not in names
        assert omitted == ["big.pdb"]

    def test_missing_source_files_silently_skipped(self, tmp_path):
        """A pruned per-frame PDB shouldn't crash the bulk download."""
        from downloads import build_pocket_zip

        a = _write_pdb(tmp_path, "present.pdb")
        missing = tmp_path / "missing.pdb"  # never created
        zip_bytes, omitted = build_pocket_zip(
            [(a, "present.pdb"), (missing, "missing.pdb")],
            [{"filename": "present.pdb"}],
            cap_bytes=10_000,
        )
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            assert "present.pdb" in names
            assert "missing.pdb" not in names

    def test_empty_metadata_skips_csv_entry(self, tmp_path):
        from downloads import build_pocket_zip

        a = _write_pdb(tmp_path, "only.pdb")
        zip_bytes, _ = build_pocket_zip(
            [(a, "only.pdb")], [], cap_bytes=10_000
        )
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert "metadata.csv" not in zf.namelist()


# ── build_complex_zip ────────────────────────────────────────────────


class TestBuildComplexZip:
    def test_zip_contains_receptor_and_ligand(self, tmp_path):
        from downloads import build_complex_zip

        rec = _write_pdb(tmp_path, "rec.pdb", size_bytes=200)
        ligand_sdf = b"ligand-sdf-content-1\n$$$$\n"
        zip_bytes = build_complex_zip(
            receptor_pdb=rec,
            ligand_sdf_bytes=ligand_sdf,
        )
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert set(zf.namelist()) == {"receptor.pdb", "ligand.sdf"}
            assert zf.read("ligand.sdf") == ligand_sdf

    def test_custom_arcnames_used(self, tmp_path):
        from downloads import build_complex_zip

        rec = _write_pdb(tmp_path, "rec.pdb")
        zip_bytes = build_complex_zip(
            receptor_pdb=rec,
            ligand_sdf_bytes=b"sdf",
            receptor_arcname="F84.pdb",
            ligand_arcname="ligX_pose.sdf",
        )
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert set(zf.namelist()) == {"F84.pdb", "ligX_pose.sdf"}

    def test_extra_metadata_lands_as_csv(self, tmp_path):
        from downloads import build_complex_zip

        rec = _write_pdb(tmp_path, "rec.pdb")
        zip_bytes = build_complex_zip(
            receptor_pdb=rec,
            ligand_sdf_bytes=b"sdf",
            extra_metadata={"affinity": -8.4, "rmsd": 1.2},
        )
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert "metadata.csv" in zf.namelist()
            rows = list(csv.DictReader(zf.read("metadata.csv").decode().splitlines()))
            assert rows == [{"affinity": "-8.4", "rmsd": "1.2"}]


# ── current_view_artifact ────────────────────────────────────────────


class TestCurrentViewArtifact:
    def test_no_ligand_pose_returns_raw_pdb(self, tmp_path):
        from downloads import MIME_PDB, current_view_artifact

        pdb = _write_pdb(tmp_path, "frame_84.pdb", size_bytes=300)
        data, filename, mime = current_view_artifact(
            structure_path=pdb,
            ligand_pose_sdf=None,
            session_short="abc123",
            frame_label="F84",
        )
        assert filename == "abc123_F84.pdb"
        assert mime == MIME_PDB
        assert data == pdb.read_bytes()

    def test_with_ligand_pose_returns_zip(self, tmp_path):
        from downloads import MIME_ZIP, current_view_artifact

        pdb = _write_pdb(tmp_path, "frame_84.pdb", size_bytes=300)
        sdf_text = "ligX\nM  END\n$$$$\n"
        data, filename, mime = current_view_artifact(
            structure_path=pdb,
            ligand_pose_sdf=sdf_text,
            session_short="abc123",
            frame_label="F84",
            ligand_label="ligX",
        )
        assert mime == MIME_ZIP
        assert filename == "abc123_F84_ligX_complex.zip"
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            assert "abc123_F84.pdb" in names
            assert "abc123_F84_ligX_pose.sdf" in names
            assert zf.read("abc123_F84_ligX_pose.sdf").decode() == sdf_text

    def test_filename_omits_ligand_suffix_when_unlabeled(self, tmp_path):
        from downloads import current_view_artifact

        pdb = _write_pdb(tmp_path, "frame_84.pdb")
        _, filename, _ = current_view_artifact(
            structure_path=pdb,
            ligand_pose_sdf="sdf",
            session_short="abc123",
            frame_label="F84",
            ligand_label=None,
        )
        # No ligand_label → just "_complex.zip" (no double underscore).
        assert filename == "abc123_F84_complex.zip"

    def test_session_short_with_underscore_does_not_double(self, tmp_path):
        """Even if a sanitised short_code contains a single ``_``, the
        filename construction must not produce ``__``."""
        from downloads import current_view_artifact

        pdb = _write_pdb(tmp_path, "frame_84.pdb")
        _, filename, _ = current_view_artifact(
            structure_path=pdb,
            ligand_pose_sdf=None,
            session_short="s_TlbKowr3xk",
            frame_label="F84",
        )
        assert "__" not in filename
