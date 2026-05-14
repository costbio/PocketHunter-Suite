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
