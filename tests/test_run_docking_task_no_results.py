"""Regression test for the docking "all pairs failed" misclassification.

Defect: when every receptor/ligand pair in ``run_docking_task`` fails (or
yields no poses), the task used to raise a bare ``ValueError``. That
classifies as ``ErrorCategory.VALIDATION`` in ``failure_view`` (see
``_EXC_TYPE_MAP``), so the failure card told the user "Input validation
failed" — implying their inputs were rejected before any work happened.
In fact smina ran every pair and all of them failed; the dedicated
``DockingProducedNoResults`` exception (and its "Docking produced no
poses" headline) already existed for exactly this case but nothing ever
raised it.

This test drives ``run_docking_task`` far enough to hit the "no docking
results generated" branch — via a receptor path that doesn't exist, so
every pair is recorded as a pair-level failure and ``list_outputs`` stays
empty — and asserts both:

  1. The exception raised is ``DockingProducedNoResults``, not ``ValueError``.
  2. Feeding that exception's type name through ``failure_view.classify_error``
     (the same lookup the UI performs from ``exc_type`` in the Celery
     FAILURE meta / status JSON) actually resolves to the NO_OUTPUT
     category and the "Docking produced no poses" headline — i.e. the
     wiring, not just the raise, is verified end-to-end.
"""
from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

import pandas as pd
import pytest


def _stub_step4_docking(monkeypatch):
    """Stand in for the real ``step4_docking`` module.

    ``step4_docking.py`` imports ``openbabel`` at module level, which isn't
    installed in this (non-conda) unit-test environment — see CLAUDE.md:
    OpenBabel is a conda-only docking dependency, pulled in only inside the
    hardened worker image. The receptor-not-found branch this test exercises
    never actually calls ``pdb_to_pdbqt`` / ``calc_box`` / ``run_smina`` /
    ``parse_smina_log``, so a bare stub with those four names is enough to
    satisfy ``tasks.py``'s ``from step4_docking import ...`` line.
    """
    stub = types.ModuleType("step4_docking")
    stub.pdb_to_pdbqt = MagicMock()
    stub.calc_box = MagicMock()
    stub.run_smina = MagicMock()
    stub.parse_smina_log = MagicMock()
    monkeypatch.setitem(sys.modules, "step4_docking", stub)


def _run_raw_docking_task(fake_self, **kwargs):
    """Call the undecorated ``run_docking_task`` function with a MagicMock
    standing in for the bound Celery task instance (``self``).

    Calling the Celery-wrapped task directly requires a live broker/backend
    (see ``.apply()`` / ``.delay()``), which isn't available in unit tests.
    ``run_docking_task.run.__func__`` is the original, undecorated function
    — calling it with an explicit fake ``self`` runs the task body
    synchronously with no Celery machinery involved. Mirrors the existing
    ``celery_task = MagicMock()`` pattern used for ``_prepare_ligands_with_progress``
    in ``tests/test_worker_writes.py``.
    """
    from tasks import run_docking_task
    raw = run_docking_task.run.__func__
    return raw(fake_self, **kwargs)


def _fake_self():
    self = MagicMock()
    self.request.id = "fake-task-id"
    return self


class TestDockingAllPairsFailedRaisesDedicatedException:
    def test_raises_docking_produced_no_results_not_value_error(self, tmp_path, monkeypatch):
        import tasks
        from task_errors import DockingProducedNoResults

        _stub_step4_docking(monkeypatch)

        # Route all disk writes (_update_status_file, output folder, live
        # log) under tmp_path instead of the real repo results/ dir.
        monkeypatch.setattr(tasks, "RESULTS_DIR", str(tmp_path))

        # One "receptor" row whose PDB file does not exist anywhere under
        # pdb_source_dir — every pair for it is recorded as a pair-level
        # failure (tasks.py's "Receptor PDB not found, skipping" branch)
        # without ever touching prody/smina.
        csv_path = tmp_path / "cluster_representatives.csv"
        pd.DataFrame([
            {"File name": "missing_receptor.pdb", "residues": "1,2,3"},
        ]).to_csv(csv_path, index=False)

        pdb_source_dir = tmp_path / "pdb_source"
        pdb_source_dir.mkdir()

        ligand_folder = tmp_path / "ligands_src"
        ligand_folder.mkdir()

        # Bypass real ligand conversion (obabel/openbabel) — stage a
        # single already-converted PDBQT straight into the work folder
        # ``run_docking_task`` expects, exactly like the real helper would.
        def _fake_prepare_ligands(celery_task, src_folder, work_folder, gen_3d=False,
                                   progress_low=2, progress_high=15):
            os.makedirs(work_folder, exist_ok=True)
            with open(os.path.join(work_folder, "ligand1.pdbqt"), "w") as fh:
                fh.write("REMARK fake ligand\n")
            return [], ["ligand1"]

        monkeypatch.setattr(tasks, "_prepare_ligands_with_progress", _fake_prepare_ligands)

        job_id = "testjob-no-results"

        with pytest.raises(DockingProducedNoResults) as excinfo:
            _run_raw_docking_task(
                _fake_self(),
                cluster_representatives_csv=str(csv_path),
                ligand_folder=str(ligand_folder),
                job_id=job_id,
                pdb_source_dir=str(pdb_source_dir),
            )

        # It must NOT be (or be a subclass masquerading as) ValueError —
        # that was the whole bug: exc_type == "ValueError" mis-triggers
        # the VALIDATION category below.
        assert not isinstance(excinfo.value, ValueError)

        # Cross-check against the actual classifier the UI calls, using
        # the exact shape the Celery FAILURE meta / status JSON uses.
        from failure_view import ErrorCategory, classify_error

        exc = excinfo.value
        classified = classify_error({
            "exc_type": type(exc).__name__,
            "exc_message": str(exc),
        })
        assert classified.category == ErrorCategory.NO_OUTPUT
        assert classified.headline == "Docking produced no poses"
