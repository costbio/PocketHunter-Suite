"""Thin facade over the pydantic-settings ``settings`` singleton.

v2 Phase A commit A4 replaced the hand-rolled ``Config`` with
``settings.Settings``. This file is preserved purely so existing call
sites (``Config.MAX_UPLOAD_SIZE``, ``Config.RESULTS_DIR``, etc.) keep
working without churn — every attribute reads from the singleton.

**New code should import ``settings`` directly:**

    from settings import settings
    print(settings.RESULTS_DIR)

The ``Config`` class is here for back-compat with the ~16 modules that
already use it. It will be retired in a future cleanup batch.
"""
from __future__ import annotations

from pathlib import Path

from settings import settings


class ConfigurationError(Exception):
    """Raised when configuration is invalid or missing required values.

    Kept for back-compat — callers can still ``except ConfigurationError``.
    The actual validation now lives in pydantic (Settings raises
    ``pydantic.ValidationError`` on invalid env), but
    ``ConfigurationError`` re-wraps it from ``Config.validate()``.
    """


class Config:
    """Attribute-compatible facade over ``settings``.

    Every property delegates to the singleton. Class-level access works
    because ``Settings`` exposes each field as an instance attribute and
    the class-level ``__getattr__`` here forwards to the singleton.
    """

    # Static class constants — same identities as v1 so reference-equality
    # against ``Config.ALLOWED_UPLOAD_EXTENSIONS`` keeps working.
    ALLOWED_UPLOAD_EXTENSIONS = settings.ALLOWED_UPLOAD_EXTENSIONS
    ALLOWED_MIME_TYPES = settings.ALLOWED_MIME_TYPES

    # Explicit class attributes pointing at the singleton's fields. We
    # bind them at class-creation time so every consumer's ``Config.X``
    # access just reads a normal class attribute (no descriptor magic).
    BASE_DIR = settings.BASE_DIR
    UPLOAD_DIR = settings.UPLOAD_DIR
    RESULTS_DIR = settings.RESULTS_DIR
    POCKETHUNTER_DIR = settings.POCKETHUNTER_DIR
    POCKETHUNTER_CLI = settings.POCKETHUNTER_CLI
    P2RANK_PATH = settings.P2RANK_PATH
    SMINA_PATH = settings.SMINA_PATH
    CELERY_BROKER_URL = settings.CELERY_BROKER_URL
    CELERY_RESULT_BACKEND = settings.CELERY_RESULT_BACKEND
    MAX_UPLOAD_SIZE = settings.MAX_UPLOAD_SIZE
    MAX_ZIP_SIZE = settings.MAX_ZIP_SIZE
    CLEANUP_AFTER_DAYS = settings.CLEANUP_AFTER_DAYS
    MAX_DISK_USAGE_GB = settings.MAX_DISK_USAGE_GB
    MAX_DOCKING_PDBS = settings.MAX_DOCKING_PDBS
    MAX_DOCKING_LIGANDS = settings.MAX_DOCKING_LIGANDS
    MAX_DOCKING_EXHAUSTIVENESS = settings.MAX_DOCKING_EXHAUSTIVENESS
    DOCKING_TIMEOUT = settings.DOCKING_TIMEOUT
    RATE_LIMIT_ENABLED = settings.RATE_LIMIT_ENABLED
    RATE_LIMIT_MAX_UPLOADS = settings.RATE_LIMIT_MAX_UPLOADS
    RATE_LIMIT_WINDOW_SECONDS = settings.RATE_LIMIT_WINDOW_SECONDS
    RATE_LIMIT_MAX_TASKS = settings.RATE_LIMIT_MAX_TASKS
    RATE_LIMIT_TASK_WINDOW_SECONDS = settings.RATE_LIMIT_TASK_WINDOW_SECONDS
    LOG_LEVEL = settings.LOG_LEVEL
    LOG_FILE = settings.LOG_FILE

    @classmethod
    def validate(cls) -> None:
        """Materialise the side effects (mkdir, PocketHunter checks).

        pydantic's ``ValidationError`` was already raised at import time
        for missing-field / type-mismatch cases. This method now only
        verifies the *runtime* invariants (filesystem layout).
        """
        try:
            settings.ensure_runtime_paths()
        except ValueError as e:
            raise ConfigurationError(str(e)) from e

    @classmethod
    def get_upload_path(cls, job_id: str, filename: str) -> Path:
        """Sanitised upload path for ``filename`` inside ``UPLOAD_DIR/<job>``."""
        safe_filename = Path(filename).name
        job_dir = settings.UPLOAD_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        return job_dir / safe_filename

    @classmethod
    def get_results_path(cls, job_id: str) -> Path:
        """``results/<job_id>/`` (created if missing)."""
        p = settings.RESULTS_DIR / job_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def get_status_file(cls, job_id: str) -> Path:
        """``results/<job_id>_status.json`` path — disk-mirror of the Job row."""
        return settings.RESULTS_DIR / f"{job_id}_status.json"

    @classmethod
    def print_config(cls) -> None:
        """Print effective settings (debugging aid)."""
        print("=" * 60)
        print("PocketHunter-Suite Configuration (v2)")
        print("=" * 60)
        for field in (
            "BASE_DIR", "UPLOAD_DIR", "RESULTS_DIR",
            "POCKETHUNTER_CLI",
            "CELERY_BROKER_URL", "DATABASE_URL", "BASE_URL",
            "LOG_LEVEL", "LOG_FILE",
            "RATE_LIMIT_ENABLED",
        ):
            print(f"{field:<22} {getattr(settings, field)}")
        print(f"MAX_UPLOAD_SIZE        {settings.MAX_UPLOAD_SIZE / (1024**2):.1f} MB")
        print(f"MAX_ZIP_SIZE           {settings.MAX_ZIP_SIZE / (1024**3):.1f} GB")
        print(f"CLEANUP_AFTER_DAYS     {settings.CLEANUP_AFTER_DAYS} days")
        print("=" * 60)


# Materialise runtime side effects on import — same behaviour as v1, so
# missing PocketHunter dirs still crash the import.
try:
    Config.validate()
except ConfigurationError as e:
    print(f"\n{'='*60}\nCONFIGURATION ERROR\n{'='*60}\n{e}\n{'='*60}\n")
    raise
