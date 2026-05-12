"""Tests for the docking_pair_failures pure helpers."""
import subprocess

import pytest

from docking_pair_failures import build_pair_failure_record, summarize_pair_failures
from task_errors import NoPosesParsed


class TestBuildPairFailureRecord:
    def test_basic_called_process_error(self):
        exc = subprocess.CalledProcessError(139, ["smina", "..."], stderr="segfault details here")
        rec = build_pair_failure_record("recA.pdb", "ligX.pdbqt", exc)
        assert rec["receptor"] == "recA.pdb"
        assert rec["ligand"] == "ligX.pdbqt"
        assert rec["exc_type"] == "CalledProcessError"
        assert "139" in rec["exc_message"]
        # CalledProcessError carries stderr — should be in stderr_tail
        assert "segfault" in rec["stderr_tail"]

    def test_generic_exception(self):
        rec = build_pair_failure_record("r.pdb", "l.pdbqt", RuntimeError("boom"))
        assert rec["exc_type"] == "RuntimeError"
        assert rec["exc_message"] == "boom"
        assert rec["stderr_tail"] == ""  # no stderr attached to generic Exception

    def test_no_poses_parsed_exception(self):
        exc = NoPosesParsed("smina exited 0 but produced no parseable poses")
        rec = build_pair_failure_record("r.pdb", "l.pdbqt", exc)
        assert rec["exc_type"] == "NoPosesParsed"
        assert "no parseable" in rec["exc_message"]

    def test_explicit_stderr_overrides_exc_attr(self):
        exc = RuntimeError("oops")
        rec = build_pair_failure_record("r", "l", exc, stderr_tail="custom stderr text")
        assert rec["stderr_tail"] == "custom stderr text"

    def test_stderr_truncated_to_max_chars(self):
        long_stderr = "x" * 5000
        exc = subprocess.CalledProcessError(1, ["smina"], stderr=long_stderr)
        rec = build_pair_failure_record("r", "l", exc, max_stderr_chars=200)
        assert len(rec["stderr_tail"]) == 200
        # Tail (not head): all 'x' here so just check length, but verify behavior with mixed text:
        exc2 = subprocess.CalledProcessError(1, ["smina"], stderr="HEAD_TEXT" + ("x" * 5000) + "TAIL_TEXT")
        rec2 = build_pair_failure_record("r", "l", exc2, max_stderr_chars=20)
        assert rec2["stderr_tail"].endswith("TAIL_TEXT")
        assert "HEAD_TEXT" not in rec2["stderr_tail"]


class TestSummarizePairFailures:
    def test_empty_list(self):
        assert summarize_pair_failures([]) == "No pair failures"

    def test_single_failure(self):
        pf = [{"exc_type": "CalledProcessError", "receptor": "r", "ligand": "l", "exc_message": "..."}]
        out = summarize_pair_failures(pf)
        assert "1 pair failed" in out
        assert "CalledProcessError" in out

    def test_multiple_same_type(self):
        pf = [
            {"exc_type": "CalledProcessError", "receptor": "r1", "ligand": "l1", "exc_message": "..."},
            {"exc_type": "CalledProcessError", "receptor": "r2", "ligand": "l2", "exc_message": "..."},
            {"exc_type": "CalledProcessError", "receptor": "r3", "ligand": "l3", "exc_message": "..."},
        ]
        out = summarize_pair_failures(pf)
        assert "3 pairs failed" in out
        # Grouped count
        assert "3× CalledProcessError" in out or "3x CalledProcessError" in out

    def test_mixed_types_grouped(self):
        pf = [
            {"exc_type": "CalledProcessError", "receptor": "r1", "ligand": "l1", "exc_message": "..."},
            {"exc_type": "CalledProcessError", "receptor": "r2", "ligand": "l2", "exc_message": "..."},
            {"exc_type": "NoPosesParsed", "receptor": "r3", "ligand": "l3", "exc_message": "..."},
        ]
        out = summarize_pair_failures(pf)
        assert "3 pairs failed" in out
        assert "CalledProcessError" in out
        assert "NoPosesParsed" in out
        # Largest group should come first
        cp_pos = out.find("CalledProcessError")
        np_pos = out.find("NoPosesParsed")
        assert cp_pos < np_pos

    def test_handles_missing_exc_type(self):
        # Defensive: if a record is malformed, the summary should still work
        pf = [{"receptor": "r", "ligand": "l", "exc_message": "msg"}]
        out = summarize_pair_failures(pf)
        assert "1 pair failed" in out
        # Unknown type bucket
        assert "Unknown" in out or "unknown" in out
