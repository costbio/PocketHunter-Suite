"""Tests for panels/docking.py's _reattach_if_running helper.

Exercises the four AsyncResult states the helper distinguishes:
STARTED → reattach + restore session_state,
SUCCESS/FAILURE → return None (caller handles results/failed),
PENDING + fresh Job → return None (Celery may not have picked up yet),
PENDING + stale Job → mark Job failed, return "lost".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


# ── _is_stale ───────────────────────────────────────────────────────────


class TestIsStale:
    def test_empty_timestamp_is_stale(self):
        from panels.docking import _is_stale
        assert _is_stale("", 60) is True

    def test_recent_timestamp_not_stale(self):
        from panels.docking import _is_stale
        recent = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        assert _is_stale(recent, threshold_s=60) is False

    def test_old_timestamp_is_stale(self):
        from panels.docking import _is_stale
        old = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
        assert _is_stale(old, threshold_s=600) is True

    def test_naive_timestamp_treated_as_utc(self):
        from panels.docking import _is_stale
        # Simulates failure_view.py:269 which calls .isoformat() on a
        # naive UTC datetime → no tz suffix in the string.
        naive_old = (datetime.utcnow() - timedelta(seconds=900)).isoformat()
        assert _is_stale(naive_old, threshold_s=600) is True

    def test_garbage_string_treated_as_stale(self):
        from panels.docking import _is_stale
        assert _is_stale("not-a-timestamp", 60) is True


# ── _reattach_if_running ────────────────────────────────────────────────


def _running_job(task_id="t-abc", legacy_id="dock_test_001", status="running",
                 last_updated=None, bucket=None):
    return {
        "kind": "docking",
        "legacy_id": legacy_id,
        "status": status,
        "task_id": task_id,
        "last_updated": last_updated or datetime.now(timezone.utc).isoformat(),
        "result_info": {"bucket_records": bucket or [{"receptor": "r1"}]},
    }


@pytest.fixture
def fresh_session_state(monkeypatch):
    """Clear docking_* keys before each test so assertions are independent."""
    import streamlit as st
    for key in list(st.session_state.keys()):
        if str(key).startswith("docking_"):
            del st.session_state[key]
    yield


class TestReattachIfRunning:
    def test_completed_job_returns_none(self, fresh_session_state):
        from panels.docking import _reattach_if_running
        job = _running_job(status="completed")
        assert _reattach_if_running(job) is None

    def test_failed_job_returns_none(self, fresh_session_state):
        from panels.docking import _reattach_if_running
        job = _running_job(status="failed")
        assert _reattach_if_running(job) is None

    def test_missing_task_id_marks_lost(self, fresh_session_state):
        from panels.docking import _reattach_if_running
        job = _running_job(task_id=None)
        with patch("db.jobs.update_by_legacy_id") as mock_update:
            assert _reattach_if_running(job) == "lost"
            mock_update.assert_called_once()
            kwargs = mock_update.call_args.kwargs
            assert kwargs["status"] == "failed"
            assert kwargs["error"]["exc_type"] == "WorkerLost"

    def test_started_state_reattaches_and_restores_state(self, fresh_session_state):
        """The happy path: Celery has the task, panel rehydrates state."""
        import streamlit as st

        from panels.docking import _reattach_if_running

        bucket = [{"receptor": "r1"}, {"receptor": "r2"}]
        job = _running_job(bucket=bucket, legacy_id="dock_xyz_42")

        with patch("celery_app.celery_app"), \
             patch("panels.docking.AsyncResult", create=True) as mock_ar_cls:
            mock_result = MagicMock()
            mock_result.state = "STARTED"
            mock_ar_cls.return_value = mock_result
            # Patch inside the import path the helper uses (it does a
            # lazy import of AsyncResult from celery.result).
            with patch("celery.result.AsyncResult", return_value=mock_result):
                outcome = _reattach_if_running(job)

        assert outcome == "reattached"
        assert st.session_state["docking_task_id"] == "t-abc"
        assert st.session_state["docking_job_id"] == "dock_xyz_42"
        assert st.session_state["docking_status"] == "running"
        assert st.session_state["docking_running_bucket"] == bucket
        assert "dock_xyz_42" in st.session_state["docking_running_results_dir"]

    def test_progress_state_reattaches(self, fresh_session_state):
        from panels.docking import _reattach_if_running
        job = _running_job()
        mock_result = MagicMock()
        mock_result.state = "PROGRESS"
        with patch("celery.result.AsyncResult", return_value=mock_result):
            assert _reattach_if_running(job) == "reattached"

    def test_success_state_returns_none(self, fresh_session_state):
        from panels.docking import _reattach_if_running
        job = _running_job()  # status='running' per DB but Celery says done
        mock_result = MagicMock()
        mock_result.state = "SUCCESS"
        with patch("celery.result.AsyncResult", return_value=mock_result):
            assert _reattach_if_running(job) is None

    def test_pending_fresh_job_returns_none(self, fresh_session_state):
        """Just submitted — give Celery a chance to pick it up."""
        from panels.docking import _reattach_if_running
        job = _running_job()  # last_updated = now
        mock_result = MagicMock()
        mock_result.state = "PENDING"
        with patch("celery.result.AsyncResult", return_value=mock_result):
            assert _reattach_if_running(job) is None

    def test_pending_stale_job_marked_lost(self, fresh_session_state):
        """PENDING + last_updated >10 min ago → task is gone."""
        from panels.docking import _reattach_if_running
        stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
        job = _running_job(last_updated=stale_ts)
        mock_result = MagicMock()
        mock_result.state = "PENDING"
        with patch("celery.result.AsyncResult", return_value=mock_result), \
             patch("db.jobs.update_by_legacy_id") as mock_update:
            assert _reattach_if_running(job) == "lost"
            mock_update.assert_called_once()
            assert mock_update.call_args.kwargs["status"] == "failed"
            assert mock_update.call_args.kwargs["error"]["exc_type"] == "WorkerLost"

    def test_broker_unreachable_returns_none(self, fresh_session_state):
        """AsyncResult constructor raises → leave alone, don't mark failed."""
        from panels.docking import _reattach_if_running
        job = _running_job()
        with patch("celery.result.AsyncResult", side_effect=ConnectionError("redis down")):
            assert _reattach_if_running(job) is None
