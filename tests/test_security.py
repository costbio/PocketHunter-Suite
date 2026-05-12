"""Tests for FileValidator.validate_job_id — the path-traversal guard."""
import pytest

from security import FileValidator, SecurityError


class TestValidateJobIdHappy:
    """Real job-ID shapes from the suite — must pass."""

    def test_find_pockets_id(self):
        v = "find_pockets_20260111_204540_a83e83b3"
        assert FileValidator.validate_job_id(v) == v

    def test_cluster_id(self):
        v = "cluster_20251213_011228_4c51c6a5"
        assert FileValidator.validate_job_id(v) == v

    def test_demo_job(self):
        # Hand-named demo jobs that exist in the suite
        v = "cluster_demo_job"
        assert FileValidator.validate_job_id(v) == v

    def test_dash_allowed(self):
        v = "dock-test-2026"
        assert FileValidator.validate_job_id(v) == v


class TestValidateJobIdRejects:
    """Anything outside [A-Za-z0-9_-] must raise SecurityError."""

    def test_parent_traversal(self):
        with pytest.raises(SecurityError):
            FileValidator.validate_job_id("../etc/passwd")

    def test_double_dot_alone(self):
        with pytest.raises(SecurityError):
            FileValidator.validate_job_id("..")

    def test_forward_slash(self):
        with pytest.raises(SecurityError):
            FileValidator.validate_job_id("foo/bar")

    def test_backslash(self):
        with pytest.raises(SecurityError):
            FileValidator.validate_job_id("foo\\bar")

    def test_null_byte(self):
        with pytest.raises(SecurityError):
            FileValidator.validate_job_id("foo\x00bar")

    def test_empty(self):
        with pytest.raises(SecurityError):
            FileValidator.validate_job_id("")

    def test_whitespace_inside(self):
        with pytest.raises(SecurityError):
            FileValidator.validate_job_id("foo bar")

    def test_tab(self):
        with pytest.raises(SecurityError):
            FileValidator.validate_job_id("foo\tbar")

    def test_shell_metachar_semicolon(self):
        with pytest.raises(SecurityError):
            FileValidator.validate_job_id("foo;ls")

    def test_shell_metachar_pipe(self):
        with pytest.raises(SecurityError):
            FileValidator.validate_job_id("foo|cat")

    def test_dot_alone(self):
        with pytest.raises(SecurityError):
            FileValidator.validate_job_id(".")

    def test_over_length(self):
        with pytest.raises(SecurityError):
            FileValidator.validate_job_id("a" * 101)

    def test_at_length_limit_ok(self):
        v = "a" * 100
        assert FileValidator.validate_job_id(v) == v

    def test_none(self):
        with pytest.raises(SecurityError):
            FileValidator.validate_job_id(None)  # type: ignore[arg-type]

    def test_unicode_homoglyph(self):
        # Cyrillic 'а' (U+0430) looks like Latin 'a' but is outside [A-Za-z]
        with pytest.raises(SecurityError):
            FileValidator.validate_job_id("cluster_ав")
