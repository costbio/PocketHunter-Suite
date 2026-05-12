"""Test the disk → DB mirror inside ``tasks._update_status_file``.

We bypass Celery entirely; the helper writes both a status JSON file
and (when a Job row with matching ``legacy_id`` exists) the DB row.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def results_dir(tmp_path, monkeypatch):
    """Redirect ``tasks.RESULTS_DIR`` to a tmpdir so each test is isolated."""
    monkeypatch.setattr("tasks.RESULTS_DIR", str(tmp_path))
    return tmp_path


def test_mirror_creates_db_row_status_when_registered(db_with_schema, results_dir):
    """A registered Job row picks up status updates from the disk writer."""
    from db.jobs import create_for_legacy, find_by_legacy_id
    from db.sessions import create_session
    from tasks import _update_status_file

    s = create_session()
    legacy = "find_pockets_x"
    create_for_legacy(s.id, "find_pockets", legacy)

    _update_status_file(legacy, "running", step="extracting frames", task_id="celery-1")

    row = find_by_legacy_id(legacy)
    assert row.status == "running"
    assert row.step == "extracting frames"
    assert row.celery_task_id == "celery-1"

    # Disk file should also exist
    disk = Path(results_dir) / f"{legacy}_status.json"
    assert disk.exists()
    blob = json.loads(disk.read_text())
    assert blob["status"] == "running"


def test_mirror_silent_no_op_when_unregistered(db_with_schema, results_dir):
    """Tasks running without a registered Job row don't crash; disk write succeeds."""
    from tasks import _update_status_file

    legacy = "find_pockets_unregistered"
    _update_status_file(legacy, "running", step="loading")

    disk = Path(results_dir) / f"{legacy}_status.json"
    assert disk.exists()
    # No exception should have been raised. (DB lookup found nothing.)


def test_mirror_writes_error_dict_on_failure(db_with_schema, results_dir):
    """When ``error=`` is passed to ``_update_status_file``, the DB row gets it too."""
    from db.jobs import create_for_legacy, find_by_legacy_id
    from db.sessions import create_session
    from tasks import _update_status_file

    s = create_session()
    legacy = "find_pockets_fail"
    create_for_legacy(s.id, "find_pockets", legacy)

    err = {
        "exc_type": "DetectionProducedNoOutput",
        "exc_message": "p2rank crashed silently",
        "stage": "detect",
        "log_path": "/results/find_pockets_fail/error.log",
    }
    _update_status_file(legacy, "failed", step="detect failed", task_id="celery-2", error=err)

    row = find_by_legacy_id(legacy)
    assert row.status == "failed"
    assert row.error == err


def test_mirror_writes_result_info_on_completion(db_with_schema, results_dir):
    from db.jobs import create_for_legacy, find_by_legacy_id
    from db.sessions import create_session
    from tasks import _update_status_file

    s = create_session()
    legacy = "cluster_ok"
    create_for_legacy(s.id, "cluster", legacy)

    _update_status_file(
        legacy,
        "completed",
        step="Pocket clustering completed",
        result_info={"clusters_found": 5, "representatives": 5},
    )

    row = find_by_legacy_id(legacy)
    assert row.status == "completed"
    assert row.result_info["clusters_found"] == 5
