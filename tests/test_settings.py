"""Tests for the v2 pydantic ``Settings`` class.

The tests construct ``Settings`` directly (not via the module-level singleton)
so they can vary the environment without leaking between cases. ``Config``'s
import-time ``validate()`` is exercised in the broader suite by virtue of
every other test importing ``config``.
"""
from __future__ import annotations

import os

import pytest
from pydantic import ValidationError


@pytest.fixture
def base_env(monkeypatch, tmp_path):
    """Provide the two required env vars + a tmp BASE_DIR so PocketHunter
    paths aren't validated against the real repo."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("BASE_URL", "http://localhost:8501")
    # No .env file in tmpdir — keeps the test isolated from the repo .env.
    monkeypatch.chdir(tmp_path)
    yield


def test_settings_loads_with_required_env_vars(base_env):
    from settings import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.DATABASE_URL == "sqlite:///:memory:"
    assert s.BASE_URL == "http://localhost:8501"
    # Defaults
    assert s.MAX_UPLOAD_SIZE == 524_288_000
    assert s.RATE_LIMIT_ENABLED is True
    assert s.LOG_LEVEL == "INFO"


def test_missing_database_url_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("BASE_URL", "http://localhost:8501")
    monkeypatch.chdir(tmp_path)
    from settings import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings()  # type: ignore[call-arg]
    assert "DATABASE_URL" in str(exc_info.value)


def test_missing_base_url_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    from settings import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings()  # type: ignore[call-arg]
    assert "BASE_URL" in str(exc_info.value)


def test_log_level_validator_normalises_case(base_env, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    from settings import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.LOG_LEVEL == "DEBUG"


def test_log_level_validator_rejects_invalid(base_env, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "BOGUS")
    from settings import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings()  # type: ignore[call-arg]
    assert "LOG_LEVEL" in str(exc_info.value)


def test_redis_url_validator_rejects_non_redis(base_env, monkeypatch):
    monkeypatch.setenv("CELERY_BROKER_URL", "amqp://localhost")
    from settings import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings()  # type: ignore[call-arg]
    assert "CELERY_BROKER_URL" in str(exc_info.value)


def test_positive_int_validator(base_env, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_SIZE", "0")
    from settings import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings()  # type: ignore[call-arg]
    assert "MAX_UPLOAD_SIZE" in str(exc_info.value)


def test_rate_limit_enabled_parses_booleans(base_env, monkeypatch):
    from settings import Settings

    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    s = Settings()  # type: ignore[call-arg]
    assert s.RATE_LIMIT_ENABLED is False

    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    s = Settings()  # type: ignore[call-arg]
    assert s.RATE_LIMIT_ENABLED is True


def test_paths_are_resolved(base_env, monkeypatch, tmp_path):
    """UPLOAD_DIR / RESULTS_DIR should be absolute after validation."""
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("RESULTS_DIR", str(tmp_path / "results"))
    from settings import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.UPLOAD_DIR.is_absolute()
    assert s.RESULTS_DIR.is_absolute()


class TestDiskUsageWarnGbAlias:
    """DISK_USAGE_WARN_GB replaced MAX_DISK_USAGE_GB (defect D — the old
    name read as an enforced cap; it only ever fed a warning-log
    percentage, never anything that refused an upload or pruned disk
    space). The rename uses a ``validation_alias=AliasChoices(...)`` so a
    deployed host's existing ``.env`` (which sets the old name) keeps
    working without an edit. These pin that behaviour against this repo's
    actual ``Settings`` class, not just a throwaway BaseSettings."""

    def test_old_env_var_name_still_works(self, base_env, monkeypatch):
        monkeypatch.delenv("DISK_USAGE_WARN_GB", raising=False)
        monkeypatch.setenv("MAX_DISK_USAGE_GB", "55")
        from settings import Settings

        s = Settings()  # type: ignore[call-arg]
        assert s.DISK_USAGE_WARN_GB == 55

    def test_new_env_var_name_works(self, base_env, monkeypatch):
        monkeypatch.delenv("MAX_DISK_USAGE_GB", raising=False)
        monkeypatch.setenv("DISK_USAGE_WARN_GB", "77")
        from settings import Settings

        s = Settings()  # type: ignore[call-arg]
        assert s.DISK_USAGE_WARN_GB == 77

    def test_neither_set_falls_back_to_default(self, base_env, monkeypatch):
        monkeypatch.delenv("MAX_DISK_USAGE_GB", raising=False)
        monkeypatch.delenv("DISK_USAGE_WARN_GB", raising=False)
        from settings import Settings

        s = Settings()  # type: ignore[call-arg]
        assert s.DISK_USAGE_WARN_GB == 100

    def test_both_set_new_name_takes_precedence(self, base_env, monkeypatch):
        monkeypatch.setenv("MAX_DISK_USAGE_GB", "55")
        monkeypatch.setenv("DISK_USAGE_WARN_GB", "77")
        from settings import Settings

        s = Settings()  # type: ignore[call-arg]
        assert s.DISK_USAGE_WARN_GB == 77


def test_config_facade_attributes_match_settings(base_env):
    """The ``Config`` class re-exports settings under the v1 names."""
    # Need to re-import config.py with a clean Settings load. Because the
    # module is cached, force a reload here. Reload any other module that
    # captured a settings field at module-load time, so subsequent tests
    # don't see a settings singleton that's out of sync with module-level
    # constants (e.g. viewer_pipeline.MAX_VIEWER_BYTES) → would otherwise
    # surface as a baffling "viewer_pipeline.X != settings.X" diff in a
    # later test.
    import importlib
    import sys

    import settings as settings_mod
    import config as config_mod
    importlib.reload(settings_mod)
    importlib.reload(config_mod)
    if "viewer_pipeline" in sys.modules:
        importlib.reload(sys.modules["viewer_pipeline"])

    assert config_mod.Config.MAX_UPLOAD_SIZE == settings_mod.settings.MAX_UPLOAD_SIZE
    assert config_mod.Config.DATABASE_URL == settings_mod.settings.DATABASE_URL if hasattr(config_mod.Config, "DATABASE_URL") else True
    # Facade exposes the same allowed-extensions set
    assert config_mod.Config.ALLOWED_UPLOAD_EXTENSIONS == settings_mod.settings.ALLOWED_UPLOAD_EXTENSIONS
