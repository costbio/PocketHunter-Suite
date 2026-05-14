"""Tests for ``live_log`` — the B8 helpers that back the live-log fragment."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_tail_log_returns_last_n_lines(tmp_path):
    from live_log import tail_log

    p = tmp_path / "log.txt"
    p.write_text("\n".join(f"line {i}" for i in range(100)) + "\n")

    out = tail_log(p, max_lines=10)
    lines = out.splitlines()
    assert len(lines) == 10
    assert lines[0] == "line 90"
    assert lines[-1] == "line 99"


def test_tail_log_handles_growing_file(tmp_path):
    """Subsequent calls see new content — no caching, no stale file handle."""
    from live_log import tail_log

    p = tmp_path / "growing.txt"
    p.write_text("\n".join(f"a {i}" for i in range(10)) + "\n")
    first = tail_log(p, max_lines=5).splitlines()
    assert first[-1] == "a 9"

    with open(p, "a") as fh:
        fh.write("\n".join(f"b {i}" for i in range(10)) + "\n")
    second = tail_log(p, max_lines=5).splitlines()
    assert second[-1] == "b 9"


def test_tail_log_returns_empty_for_missing_file(tmp_path):
    from live_log import tail_log

    assert tail_log(tmp_path / "no-such-file.log") == ""


def test_tail_log_handles_empty_file(tmp_path):
    from live_log import tail_log

    p = tmp_path / "empty.log"
    p.write_text("")
    assert tail_log(p) == ""


def test_sanitize_stage_name_matches_run_stage_convention():
    from live_log import sanitize_stage_name

    assert sanitize_stage_name("Detecting pockets") == "detecting_pockets"
    assert sanitize_stage_name("Extract frames") == "extract_frames"
    assert sanitize_stage_name("  Multi  spaces  ") == "multi_spaces"
    # Empty / weird inputs fall back to "stage"
    assert sanitize_stage_name("///") == "stage"
    assert sanitize_stage_name("") == "stage"


def test_any_task_running_returns_false_for_empty_session(db_with_schema):
    from db.sessions import create_session
    from live_log import any_task_running

    s = create_session()
    assert any_task_running(s.id) is False
    assert any_task_running(None) is False


def test_any_task_running_detects_running_job(db_with_schema):
    from db.jobs import create_for_legacy, update_by_legacy_id
    from db.sessions import create_session
    from live_log import any_task_running

    s = create_session()
    create_for_legacy(s.id, "find_pockets", "job_a")
    update_by_legacy_id("job_a", "running")
    assert any_task_running(s.id) is True

    update_by_legacy_id("job_a", "completed")
    assert any_task_running(s.id) is False


def test_find_active_stage_log_returns_none_when_no_running_jobs(tmp_path, db_with_schema):
    from db.sessions import create_session
    from live_log import find_active_stage_log

    s = create_session()
    assert find_active_stage_log(s.id, tmp_path) is None


def test_find_active_stage_log_finds_newest_log(tmp_path, db_with_schema):
    import time

    from db.jobs import create_for_legacy, update_by_legacy_id
    from db.sessions import create_session
    from live_log import find_active_stage_log

    s = create_session()
    legacy = "job_running"
    create_for_legacy(s.id, "find_pockets", legacy)
    update_by_legacy_id(legacy, "running")

    live_dir = tmp_path / legacy / ".live"
    live_dir.mkdir(parents=True)
    older = live_dir / "extract_frames.stdout.log"
    older.write_text("old\n")
    time.sleep(0.05)
    newer = live_dir / "detecting_pockets.stdout.log"
    newer.write_text("new\n")

    result = find_active_stage_log(s.id, tmp_path)
    assert result == newer
