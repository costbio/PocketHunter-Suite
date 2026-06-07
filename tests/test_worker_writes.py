"""Regression tests for the Phase C "workers never write to /app/uploads" rule.

The hardened worker containers bind-mount ``/app/uploads`` read-only.
Any task code that writes there silently fails (or fails with a
confusing FileNotFoundError downstream). These tests pin the two known
worker write paths so they stay rooted in ``/app/results/<job>/`` instead.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


class _FakeProc:
    """Stand-in for ``subprocess.Popen`` used by _prepare_ligands_with_progress.

    Pretends to invoke obabel. On the first ``poll()`` call it lays down
    the PDBQT files the real obabel would have produced (next to the
    input file, with ``_<N>.pdbqt`` suffixes), then reports a clean exit.
    """

    def __init__(self, cmd, **kwargs):
        # cmd shape: ["obabel", source, "-O", "<stem>_.pdbqt", "-m", ...]
        self._source = cmd[1]
        self._out_template = cmd[3]   # "<stem>_.pdbqt"
        self._stem = self._out_template[:-7]   # drop the "_.pdbqt"
        self._n_mols = 2   # write 2 PDBQTs
        self._polled = False
        self.returncode = None
        self.stderr = MagicMock()
        self.stderr.read = lambda: b""

    def poll(self):
        if not self._polled:
            self._polled = True
            for i in range(1, self._n_mols + 1):
                with open(f"{self._stem}_{i}.pdbqt", "w") as fh:
                    fh.write(f"REMARK fake PDBQT {i}\n")
            self.returncode = 0
            return 0
        return 0

    def wait(self, timeout=None):
        return 0


class TestPrepareLigandsWorkFolder:
    def _build_source(self, tmp_path):
        """Create a source/RO-style dir with one SDF holding 2 molecules."""
        src = tmp_path / "uploads" / "ligands_job1"
        src.mkdir(parents=True)
        sdf = src / "mols.sdf"
        # Two SDF records — _count_sdf_molecules counts ``$$$$`` delimiters.
        sdf.write_text(
            "mol1\n  RDKit\n\n  1  0  0  0  0  0  0  0  0  0999 V2000\n"
            "    0.0000    0.0000    0.0000 C   0  0  0\nM  END\n$$$$\n"
            "mol2\n  RDKit\n\n  1  0  0  0  0  0  0  0  0  0999 V2000\n"
            "    0.0000    0.0000    0.0000 C   0  0  0\nM  END\n$$$$\n"
        )
        return src

    def test_pdbqt_outputs_land_in_work_folder_not_source(self, tmp_path):
        from tasks import _prepare_ligands_with_progress

        src = self._build_source(tmp_path)
        work = tmp_path / "results" / "job1" / "ligands"

        celery_task = MagicMock()
        with patch("subprocess.Popen", _FakeProc):
            failures, names = _prepare_ligands_with_progress(
                celery_task, str(src), str(work),
            )

        # Work dir has the converted PDBQTs.
        work_pdbqts = sorted(p.name for p in work.glob("*.pdbqt"))
        assert work_pdbqts == ["mols_1.pdbqt", "mols_2.pdbqt"]

        # Source dir is untouched (RO contract): the original SDF
        # remains, no PDBQT files created there.
        src_files = sorted(p.name for p in src.iterdir())
        assert src_files == ["mols.sdf"]
        assert list(src.glob("*.pdbqt")) == []
        assert not failures

    def test_already_pdbqt_inputs_are_copied_through(self, tmp_path):
        from tasks import _prepare_ligands_with_progress

        src = tmp_path / "uploads" / "ligands_job1"
        src.mkdir(parents=True)
        (src / "already.pdbqt").write_text("REMARK pre-converted\n")
        work = tmp_path / "results" / "job1" / "ligands"

        celery_task = MagicMock()
        with patch("subprocess.Popen", _FakeProc):
            _prepare_ligands_with_progress(celery_task, str(src), str(work))

        assert (work / "already.pdbqt").exists()
        # Source untouched.
        assert (src / "already.pdbqt").exists()

    def test_empty_source_returns_clean(self, tmp_path):
        from tasks import _prepare_ligands_with_progress

        src = tmp_path / "uploads" / "ligands_job1"
        src.mkdir(parents=True)
        work = tmp_path / "results" / "job1" / "ligands"

        celery_task = MagicMock()
        failures, names = _prepare_ligands_with_progress(
            celery_task, str(src), str(work),
        )
        assert failures == []
        assert names == {}

    def test_ligand_names_json_written_to_work_folder(self, tmp_path):
        from tasks import _prepare_ligands_with_progress

        src = self._build_source(tmp_path)
        work = tmp_path / "results" / "job1" / "ligands"

        celery_task = MagicMock()
        with patch("subprocess.Popen", _FakeProc):
            _prepare_ligands_with_progress(celery_task, str(src), str(work))

        assert (work / "ligand_names.json").exists()
        # And NOT in the RO source dir.
        assert not (src / "ligand_names.json").exists()


class TestFindPocketsPdbDirStaging:
    """The pdb_dir branch of run_find_pockets_task copies PDBs from the
    RO uploads dir into results/<job>/pdbs before writing pdb_list.ds.

    This validates the helper that performs the listing step — the
    full task body is integration-tested by the actual run path; here
    we just confirm ``write_pdb_list_for_detect`` operates correctly
    when the target dir is the writable copy.
    """

    def test_pdb_list_written_in_target_dir(self, tmp_path):
        from find_pockets_helpers import write_pdb_list_for_detect

        # Mimic the post-staging state: a writable results/<job>/pdbs
        # directory with a couple of PDB copies.
        results_pdb_dir = tmp_path / "results" / "job1" / "pdbs"
        results_pdb_dir.mkdir(parents=True)
        for name in ("frame_001.pdb", "frame_002.pdb"):
            (results_pdb_dir / name).write_text("ATOM\n")

        out = write_pdb_list_for_detect(results_pdb_dir)
        assert out == results_pdb_dir / "pdb_list.ds"
        assert out.read_text().splitlines() == ["frame_001.pdb", "frame_002.pdb"]
