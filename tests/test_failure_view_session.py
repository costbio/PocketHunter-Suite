"""Tests for ``failure_view.load_status_for_session`` — the v2 read side."""
from __future__ import annotations


def test_load_status_for_session_returns_status_dicts(db_with_schema):
    from db.jobs import create_for_legacy, update_by_legacy_id
    from db.sessions import create_session
    from failure_view import load_status_for_session

    s = create_session()
    create_for_legacy(s.id, "find_pockets", "fp_1")
    create_for_legacy(s.id, "cluster", "cluster_1")

    update_by_legacy_id("fp_1", "completed", result_info={"pockets_detected": 12})
    update_by_legacy_id(
        "cluster_1",
        "failed",
        error={"exc_type": "ClusteringFoundNoClusters", "exc_message": "0 clusters", "stage": "cluster"},
    )

    statuses = load_status_for_session(s.id)
    assert len(statuses) == 2
    by_kind = {d["kind"]: d for d in statuses}
    assert by_kind["find_pockets"]["status"] == "completed"
    assert by_kind["find_pockets"]["result_info"] == {"pockets_detected": 12}
    assert by_kind["cluster"]["status"] == "failed"
    assert by_kind["cluster"]["error"]["exc_type"] == "ClusteringFoundNoClusters"


def test_load_status_for_session_empty_when_no_jobs(db_with_schema):
    from db.sessions import create_session
    from failure_view import load_status_for_session

    s = create_session()
    assert load_status_for_session(s.id) == []


def test_load_status_for_session_silent_on_none(db_with_schema):
    from failure_view import load_status_for_session

    assert load_status_for_session(None) == []
