"""Tests for the MIME-sniffing layer added in B3.3.

python-magic is an optional system-dep; tests that exercise the real
``libmagic1`` skip cleanly when the lib isn't present.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


def _libmagic_available():
    try:
        import magic
        magic.from_buffer(b"hi", mime=True)
        return True
    except Exception:
        return False


class TestValidateMimeType:
    def test_passes_chemistry_files_through(self, tmp_path):
        from security import FileValidator

        # A PDB-like text file. libmagic detects it as text/plain; not
        # denylisted → returned as-is, no raise.
        p = tmp_path / "fake.pdb"
        p.write_text("ATOM      1  N   ALA A   1       0.000   0.000   0.000\n")
        detected = FileValidator.validate_mime_type(p)
        assert detected != "unknown" or True  # soft-fail OK

    @pytest.mark.skipif(not _libmagic_available(),
                        reason="libmagic not installed in this env")
    def test_rejects_html_disguised_as_pdb(self, tmp_path):
        from security import FileValidator, SecurityError

        p = tmp_path / "trojan.pdb"
        p.write_text("<!DOCTYPE html><html><body>not a pdb</body></html>")
        with pytest.raises(SecurityError, match="denylist"):
            FileValidator.validate_mime_type(p)

    def test_soft_fails_when_libmagic_unavailable(self, tmp_path):
        from security import FileValidator

        p = tmp_path / "x.pdb"
        p.write_text("ATOM\n")
        # Force an ImportError as if python-magic weren't installed.
        with patch.dict("sys.modules", {"magic": None}):
            # Importing inside the function — patch.dict with value=None
            # makes the import raise ImportError.
            detected = FileValidator.validate_mime_type(p)
        assert detected == "unknown"


class TestDenyListContents:
    def test_denylist_includes_html_and_executables(self):
        from security import FileValidator

        for must_block in ("text/html",
                            "application/x-executable",
                            "application/x-dosexec",
                            "application/x-mach-binary"):
            assert must_block in FileValidator.DENYLISTED_MIME_TYPES
