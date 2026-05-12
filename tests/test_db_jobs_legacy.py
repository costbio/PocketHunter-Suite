"""Tests for the v1↔v2 ``legacy_id`` bridge on the jobs table."""
from __future__ import annotations

import uuid

import pytest


def test_create_for_legacy_tags_the_row(db_with_schema):
    from db.jobs import create_for_legacy, find_by_legacy_id
    from db.sessions import create_session

    s = create_session()
    legacy = "find_pockets_20260512_120000_abcd1234"
    job = create_for_legacy(s.id, "find_pockets", legacy)
    assert job.legacy_id == legacy
    assert job.kind == "find_pockets"
    assert job.session_id == s.id

    looked_up = find_by_legacy_id(legacy)
    assert looked_up is not None
    assert looked_up.id == job.id


def test_find_by_legacy_id_returns_none_on_miss(db_with_schema):
    from db.jobs import find_by_legacy_id

    assert find_by_legacy_id("nope-no-such-job") is None


def test_update_by_legacy_id_round_trip(db_with_schema):
    from db.jobs import create_for_legacy, find_by_legacy_id, update_by_legacy_id
    from db.sessions import create_session

    s = create_session()
    create_for_legacy(s.id, "cluster", "cluster_x")

    updated = update_by_legacy_id(
        "cluster_x",
        "running",
        step="DBSCAN clustering pockets",
        celery_task_id="celery-task-99",
        result_info={"pockets_filtered": 240},
    )
    assert updated is True

    row = find_by_legacy_id("cluster_x")
    assert row.status == "running"
    assert row.step == "DBSCAN clustering pockets"
    assert row.celery_task_id == "celery-task-99"
    assert row.result_info == {"pockets_filtered": 240}


def test_update_by_legacy_id_no_match_returns_false(db_with_schema):
    """Silent no-op — used by the task layer's mirror path."""
    from db.jobs import update_by_legacy_id

    assert update_by_legacy_id("unregistered_job", "running") is False


def test_update_by_legacy_id_preserves_unspecified_fields(db_with_schema):
    """None means 'leave alone', not 'set to NULL'."""
    from db.jobs import create_for_legacy, find_by_legacy_id, update_by_legacy_id
    from db.sessions import create_session

    s = create_session()
    create_for_legacy(s.id, "docking", "dock_y", celery_task_id="orig-celery")
    update_by_legacy_id("dock_y", "completed", result_info={"poses": 5})

    row = find_by_legacy_id("dock_y")
    assert row.status == "completed"
    assert row.result_info == {"poses": 5}
    # celery_task_id was not passed — should still be the original value.
    assert row.celery_task_id == "orig-celery"


def test_update_propagates_error_and_pair_failures(db_with_schema):
    from db.jobs import create_for_legacy, find_by_legacy_id, update_by_legacy_id
    from db.sessions import create_session

    s = create_session()
    create_for_legacy(s.id, "docking", "dock_z")

    update_by_legacy_id(
        "dock_z",
        "completed",
        result_info={"pairs_total": 6, "pairs_succeeded": 4, "pairs_failed": 2},
        pair_failures=[
            {"receptor": "A.pdb", "ligand": "L.pdbqt", "exc_type": "CalledProcessError"},
            {"receptor": "B.pdb", "ligand": "L.pdbqt", "exc_type": "NoPosesParsed"},
        ],
        pair_failures_log="/results/dock_z/docking_pair_failures.log",
    )

    row = find_by_legacy_id("dock_z")
    assert row.pair_failures is not None
    assert len(row.pair_failures) == 2
    assert row.pair_failures_log.endswith("docking_pair_failures.log")


def test_failure_dict_round_trip(db_with_schema):
    """The 'error' JSONB column carries the failure_view dict from F3."""
    from db.jobs import create_for_legacy, find_by_legacy_id, update_by_legacy_id
    from db.sessions import create_session

    s = create_session()
    create_for_legacy(s.id, "find_pockets", "fp_fail")

    err = {
        "exc_type": "DetectionProducedNoOutput",
        "exc_message": "p2rank produced no usable output",
        "stage": "detect",
        "log_path": "/results/fp_fail/error.log",
    }
    update_by_legacy_id("fp_fail", "failed", step="detect failed", error=err)

    row = find_by_legacy_id("fp_fail")
    assert row.status == "failed"
    assert row.error == err
