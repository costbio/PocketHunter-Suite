"""Tests for ``viewer_pipeline.link_viewer_for_session``.

The helper materialises a session's ``viewer.cif`` under
``<base_dir>/static/<short>/viewer.cif`` so Streamlit's static-files
route can serve it to the Mol* component.

**As of post-B9** the helper always **copies** the source file. The
earlier symlink path was rejected by Streamlit's static handler (HTTP
400 — security policy against symlinks pointing outside ``./static/``).
The four tests below cover the behavioural contract:

* fresh dir → real file is created (not a symlink);
* identical re-call → no-op (idempotent, no re-copy);
* re-call with a different source → target's content follows the new source;
* re-call after the source file is updated → target re-copies.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


SHORT = "abc123def456"


def _make_source(tmp_path: Path, name: str, content: bytes = b"viewer-cif") -> Path:
    """Write a fake viewer.cif at tmp_path/<name>/viewer.cif and return its path."""
    src_dir = tmp_path / name
    src_dir.mkdir()
    src = src_dir / "viewer.cif"
    src.write_bytes(content)
    return src


def test_link_creates_real_file(tmp_path):
    from viewer_pipeline import VIEWER_FILE_NAME, link_viewer_for_session

    src = _make_source(tmp_path, "src_a", b"frames-A")
    base_dir = tmp_path / "repo"
    base_dir.mkdir()

    url = link_viewer_for_session(SHORT, src, base_dir)

    target = base_dir / "static" / SHORT / VIEWER_FILE_NAME
    assert target.exists(), "target must be created"
    assert not target.is_symlink(), "must be a real file, not a symlink"
    assert target.read_bytes() == b"frames-A"
    assert url.endswith(f"/{SHORT}/{VIEWER_FILE_NAME}")
    assert url.startswith("/app/static/")


def test_link_is_idempotent(tmp_path):
    from viewer_pipeline import VIEWER_FILE_NAME, link_viewer_for_session

    src = _make_source(tmp_path, "src", b"frames")
    base_dir = tmp_path / "repo"
    base_dir.mkdir()

    url_1 = link_viewer_for_session(SHORT, src, base_dir)
    target = base_dir / "static" / SHORT / VIEWER_FILE_NAME
    mtime_before = target.stat().st_mtime

    # Same source → second call detects size+mtime match and returns early.
    url_2 = link_viewer_for_session(SHORT, src, base_dir)
    mtime_after = target.stat().st_mtime

    assert url_1 == url_2
    assert mtime_before == mtime_after, "idempotent path should not recopy"
    assert target.read_bytes() == b"frames"


def test_link_updates_when_target_changes(tmp_path):
    """Different source files (distinguishable by size) → target follows."""
    from viewer_pipeline import VIEWER_FILE_NAME, link_viewer_for_session

    # Sizes differ so the helper's size+mtime idempotency check correctly
    # detects "this is a new file" even on filesystems that batch mtimes
    # at the same nanosecond for back-to-back writes.
    src_a = _make_source(tmp_path, "src_a", b"A" * 100)
    src_b = _make_source(tmp_path, "src_b", b"B" * 200)
    base_dir = tmp_path / "repo"
    base_dir.mkdir()

    link_viewer_for_session(SHORT, src_a, base_dir)
    target = base_dir / "static" / SHORT / VIEWER_FILE_NAME
    assert target.stat().st_size == 100

    link_viewer_for_session(SHORT, src_b, base_dir)
    assert target.stat().st_size == 200
    assert target.read_bytes() == b"B" * 200


def test_link_replaces_stale_symlink_from_old_versions(tmp_path):
    """Pre-B9 versions left symlinks in static/<short>/. The new helper
    must replace those with a real copy, not leave them broken."""
    from viewer_pipeline import VIEWER_FILE_NAME, link_viewer_for_session

    src = _make_source(tmp_path, "src", b"frames")
    base_dir = tmp_path / "repo"
    static_dir = base_dir / "static" / SHORT
    static_dir.mkdir(parents=True)

    # Plant a stale symlink (as pre-B9 would have).
    bogus_target = tmp_path / "no-such-file.cif"
    os.symlink(bogus_target, static_dir / VIEWER_FILE_NAME)
    assert (static_dir / VIEWER_FILE_NAME).is_symlink()

    link_viewer_for_session(SHORT, src, base_dir)

    target = static_dir / VIEWER_FILE_NAME
    assert not target.is_symlink(), "stale symlink must be replaced with a copy"
    assert target.read_bytes() == b"frames"
