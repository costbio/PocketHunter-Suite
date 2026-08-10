"""Tests for the failure_view classifier — exc_type/exc_message → category + headline + suggestion."""
import pytest

from failure_view import ErrorCategory, classify_error


def _info(exc_type, exc_message, **extra):
    return {"exc_type": exc_type, "exc_message": exc_message, **extra}


class TestClassifyValidation:
    def test_value_error_input_validation(self):
        r = classify_error(_info("ValueError", "Trajectory mode requires BOTH xtc_file_path and topology_file_path."))
        assert r.category == ErrorCategory.VALIDATION
        assert "input" in r.headline.lower() or "validat" in r.headline.lower()
        assert r.suggestion  # non-empty actionable suggestion

    def test_value_error_csv_columns_missing(self):
        r = classify_error(_info("ValueError", "Required columns missing: ['File name', 'residues']"))
        assert r.category == ErrorCategory.VALIDATION


class TestClassifyTimeout:
    def test_soft_time_limit_exceeded(self):
        r = classify_error(_info("SoftTimeLimitExceeded", "Soft time limit (3600s) exceeded"))
        assert r.category == ErrorCategory.TIMEOUT
        assert "timeout" in r.headline.lower() or "time" in r.headline.lower()
        assert "stride" in r.suggestion.lower() or "smaller" in r.suggestion.lower() or "param" in r.suggestion.lower()

    def test_timeout_expired(self):
        r = classify_error(_info("TimeoutExpired", "Command 'python pockethunter.py extract_to_pdb' timed out after 1800 seconds"))
        assert r.category == ErrorCategory.TIMEOUT


class TestClassifyNoOutput:
    def test_detection_produced_no_output(self):
        r = classify_error(_info("DetectionProducedNoOutput", "Pocket detection produced no pockets.csv"))
        assert r.category == ErrorCategory.NO_OUTPUT
        assert "p2rank" in r.suggestion.lower() or "detection" in r.suggestion.lower()

    def test_clustering_found_no_clusters(self):
        r = classify_error(_info("ClusteringFoundNoClusters", "DBSCAN found no clusters at min_prob=0.5"))
        assert r.category == ErrorCategory.NO_OUTPUT
        # Specific suggestion bits: lower min_prob, lower stride, switch to Hierarchical
        s = r.suggestion.lower()
        assert "min_prob" in s or "threshold" in s or "hierarchical" in s

    def test_docking_produced_no_results(self):
        r = classify_error(_info("DockingProducedNoResults", "No docking poses produced for any receptor/ligand pair"))
        assert r.category == ErrorCategory.NO_OUTPUT


class TestClassifySubprocessCrash:
    def test_generic_exception_with_stderr_tail(self):
        msg = "Detecting pockets failed (exit 137). Stderr: java.lang.OutOfMemoryError: Java heap space"
        r = classify_error(_info("Exception", msg))
        assert r.category == ErrorCategory.SUBPROCESS_CRASH
        # Should surface that this looks like an OOM
        assert "memory" in r.suggestion.lower() or "oom" in r.suggestion.lower() or "smaller" in r.suggestion.lower()

    def test_called_process_error_smina_segfault(self):
        r = classify_error(_info("CalledProcessError", "Command 'smina' returned non-zero exit status 139."))
        assert r.category == ErrorCategory.SUBPROCESS_CRASH


class TestClassifyDependencyMissing:
    def test_missing_pdbqt(self):
        r = classify_error(_info("FileNotFoundError", "No ligand PDBQT files found in /tmp/ligands"))
        assert r.category == ErrorCategory.DEPENDENCY_MISSING

    def test_missing_smina_binary(self):
        r = classify_error(_info("FileNotFoundError", "[Errno 2] No such file or directory: '/usr/local/bin/smina'"))
        assert r.category == ErrorCategory.DEPENDENCY_MISSING
        assert "smina" in r.suggestion.lower() or "install" in r.suggestion.lower()


class TestClassifyWorkerLost:
    def test_stale_job_reaped(self):
        r = classify_error(_info(
            "StaleJobReaped",
            "This find_pockets job was still 'running' after 306.7h with no "
            "update from any worker — longer than a find_pockets job can "
            "legitimately take (1.8h). The worker that picked it up almost "
            "certainly died before it could record success or failure.",
        ))
        assert r.category == ErrorCategory.WORKER_LOST
        # Must NOT get the generic "bug report" copy — there is no bug here.
        assert "bug report" not in r.suggestion.lower()
        assert "task failed" not in r.headline.lower()
        assert "resubmit" in r.suggestion.lower()


class TestClassifyUnknown:
    def test_unknown_falls_through(self):
        r = classify_error(_info("WeirdCustomError", "something exploded in a way we don't recognise"))
        assert r.category == ErrorCategory.UNKNOWN
        # Still produces a non-empty headline + suggestion (graceful fallback)
        assert r.headline and r.suggestion

    def test_empty_info_handled(self):
        r = classify_error(None)
        assert r.category == ErrorCategory.UNKNOWN

    def test_missing_keys_handled(self):
        r = classify_error({})
        assert r.category == ErrorCategory.UNKNOWN
