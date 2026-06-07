"""Tests for ``streamlit_gzip_static`` — transparent gzip for large
``/app/static/`` files.

The actual endpoint behaviour is hard to exercise without booting
Streamlit's server; we test the building blocks (``_ensure_gz_sibling``
and the monkey-patched ``FileResponse`` subclass) directly.
"""
from __future__ import annotations

import gzip
import os
from pathlib import Path


def test_ensure_gz_sibling_creates_compressed_copy(tmp_path):
    from streamlit_gzip_static import _ensure_gz_sibling

    src = tmp_path / "viewer.pdb"
    src.write_text("ATOM" * 100_000)  # 400 KB of text
    gz = _ensure_gz_sibling(str(src))
    assert gz == str(src) + ".gz"
    assert Path(gz).exists()
    # Round-trip the gz to confirm it's valid compressed PDB.
    with gzip.open(gz, "rt") as f:
        assert f.read() == src.read_text()


def test_ensure_gz_sibling_is_idempotent(tmp_path):
    from streamlit_gzip_static import _ensure_gz_sibling

    src = tmp_path / "viewer.pdb"
    src.write_text("hello")
    first = _ensure_gz_sibling(str(src))
    mtime_first = os.path.getmtime(first)
    # Second call must not rewrite the gz (mtime unchanged) when source
    # is older than the gz.
    second = _ensure_gz_sibling(str(src))
    assert first == second
    assert os.path.getmtime(second) == mtime_first


def test_ensure_gz_sibling_rebuilds_when_source_newer(tmp_path):
    """If the worker rewrites viewer.pdb, the stale gz must be regenerated."""
    from streamlit_gzip_static import _ensure_gz_sibling

    src = tmp_path / "viewer.pdb"
    src.write_text("first")
    gz = _ensure_gz_sibling(str(src))
    first_gz_mtime = os.path.getmtime(gz)

    # Modify source AFTER the gz; bump mtime by 2s to outrun fs resolution.
    src.write_text("second-now-changed")
    new_mtime = first_gz_mtime + 2
    os.utime(src, (new_mtime, new_mtime))

    _ensure_gz_sibling(str(src))
    with gzip.open(gz, "rt") as f:
        assert f.read() == "second-now-changed"


def test_install_gzip_static_is_idempotent_and_safe():
    """Calling install twice must not double-wrap. Defensive guard."""
    from streamlit.web.server.starlette import starlette_routes
    from streamlit_gzip_static import install_gzip_static

    install_gzip_static()
    first_class = starlette_routes.FileResponse
    install_gzip_static()
    second_class = starlette_routes.FileResponse
    assert first_class is second_class


def test_install_patches_file_response_subclass():
    from streamlit.web.server.starlette import starlette_routes
    from starlette.responses import FileResponse
    from streamlit_gzip_static import install_gzip_static

    install_gzip_static()
    assert issubclass(starlette_routes.FileResponse, FileResponse)
    assert starlette_routes.FileResponse is not FileResponse
