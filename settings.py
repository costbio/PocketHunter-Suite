"""Application-wide settings driven by pydantic-settings (v2 Phase A commit A4).

This is the single source of truth for every runtime knob. Every consumer
either imports the ``settings`` singleton directly::

    from settings import settings
    print(settings.MAX_UPLOAD_SIZE)

…or goes through the thin ``Config`` facade in ``config.py`` (kept for
call-site stability with v1 code; new code should prefer ``settings``).

Required fields (no default — app refuses to start without them):
    * ``DATABASE_URL`` — Postgres / SQLite connection string.
    * ``BASE_URL`` — public origin used to generate ``/s/<short>?edit=…``
      share URLs.

Everything else has a sensible default that matches the v1 ``Config`` class.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_DEFAULT_BASE_DIR = Path(__file__).parent.resolve()


class Settings(BaseSettings):
    """v2 application settings. Loaded from environment (with .env support)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ── Base directories ─────────────────────────────────────────────────
    BASE_DIR: Path = Field(default=_DEFAULT_BASE_DIR)
    UPLOAD_DIR: Path = Field(default=_DEFAULT_BASE_DIR / "uploads")
    RESULTS_DIR: Path = Field(default=_DEFAULT_BASE_DIR / "results")

    # ── PocketHunter CLI ─────────────────────────────────────────────────
    POCKETHUNTER_DIR: Path = Field(default=_DEFAULT_BASE_DIR / "PocketHunter")
    POCKETHUNTER_CLI: Path = Field(default=_DEFAULT_BASE_DIR / "PocketHunter" / "pockethunter.py")

    # ── External tools ───────────────────────────────────────────────────
    P2RANK_PATH: str = Field(default="prank")
    SMINA_PATH: str = Field(default="smina")

    # ── Celery / Redis ───────────────────────────────────────────────────
    CELERY_BROKER_URL: str = Field(default="redis://localhost:6379/0")
    CELERY_RESULT_BACKEND: str = Field(default="redis://localhost:6379/0")

    # ── v2: persistence + URL routing (REQUIRED — no fallback) ───────────
    DATABASE_URL: str = Field(...)
    BASE_URL: str = Field(...)

    # ── File upload limits (bytes) ───────────────────────────────────────
    MAX_UPLOAD_SIZE: int = Field(default=524_288_000)   # 500 MB
    MAX_ZIP_SIZE: int = Field(default=1_073_741_824)    # 1 GB

    # ── Resource management ──────────────────────────────────────────────
    CLEANUP_AFTER_DAYS: int = Field(default=30)
    MAX_DISK_USAGE_GB: int = Field(default=100)
    MAX_DOCKING_PDBS: int = Field(default=20)
    MAX_DOCKING_LIGANDS: int = Field(default=10)
    MAX_DOCKING_EXHAUSTIVENESS: int = Field(default=12)
    DOCKING_TIMEOUT: int = Field(default=7200)  # 2 hours

    # ── Worker & compute tuning (B11.21) ─────────────────────────────────
    # Celery worker concurrency — read by docker-compose's worker
    # ``command:`` lines (must live in .env for compose interpolation).
    WORKER_CONCURRENCY: int = Field(default=8)        # pipeline queue
    DOCKING_CONCURRENCY: int = Field(default=4)       # docking queue
    # p2rank thread count for the detect_pockets stage — not user-facing.
    P2RANK_THREADS: int = Field(default=4)
    # Fixed smina exhaustiveness — not user-facing (no slider).
    DOCKING_EXHAUSTIVENESS: int = Field(default=8)
    # Max ligand×pocket pairs per docking run — an SDF is rejected when
    # ``molecules × pockets`` exceeds this.
    DOCKING_MAX_PAIRS: int = Field(default=1000)
    # Cap on the on-demand "all docking results" ZIP download (bytes).
    MAX_DOWNLOAD_ZIP_SIZE: int = Field(default=536_870_912)  # 512 MB

    # ── Rate limiting ────────────────────────────────────────────────────
    RATE_LIMIT_ENABLED: bool = Field(default=True)
    RATE_LIMIT_MAX_UPLOADS: int = Field(default=10)
    RATE_LIMIT_WINDOW_SECONDS: int = Field(default=60)
    RATE_LIMIT_MAX_TASKS: int = Field(default=5)
    RATE_LIMIT_TASK_WINDOW_SECONDS: int = Field(default=60)

    # ── Logging ──────────────────────────────────────────────────────────
    LOG_LEVEL: str = Field(default="INFO")
    LOG_FILE: Path = Field(default=_DEFAULT_BASE_DIR / "pockethunter-suite.log")

    # ── Static (not env-driven) ──────────────────────────────────────────
    # Frozen sets — same shape as v1 Config. Class-level constants because
    # there's no good reason to make them tunable from .env.
    ALLOWED_UPLOAD_EXTENSIONS: frozenset[str] = frozenset({
        ".xtc", ".pdb", ".gro", ".csv", ".zip", ".sdf", ".pdbqt",
    })
    ALLOWED_MIME_TYPES: frozenset[str] = frozenset({
        "application/octet-stream",
        "chemical/x-pdb",
        "text/csv",
        "application/zip",
        "chemical/x-mdl-sdfile",
        "text/plain",
    })

    # ── Validators ───────────────────────────────────────────────────────

    @field_validator("UPLOAD_DIR", "RESULTS_DIR", mode="before")
    @classmethod
    def _resolve_path(cls, v):
        return Path(v).resolve() if v else v

    @field_validator("LOG_LEVEL")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(
                f"Invalid LOG_LEVEL: {v!r}. Valid options: {', '.join(sorted(valid))}"
            )
        return upper

    @field_validator("CELERY_BROKER_URL")
    @classmethod
    def _validate_redis_url(cls, v: str) -> str:
        if not v.startswith("redis://"):
            raise ValueError(
                f"Invalid CELERY_BROKER_URL: {v!r}. Expected format: redis://host:port/db"
            )
        return v

    @field_validator("MAX_UPLOAD_SIZE", "MAX_ZIP_SIZE", "CLEANUP_AFTER_DAYS",
                     "MAX_DISK_USAGE_GB", "WORKER_CONCURRENCY",
                     "DOCKING_CONCURRENCY", "P2RANK_THREADS",
                     "DOCKING_EXHAUSTIVENESS", "DOCKING_MAX_PAIRS",
                     "MAX_DOWNLOAD_ZIP_SIZE")
    @classmethod
    def _positive_int(cls, v: int, info) -> int:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got: {v}")
        return v

    # ── Methods ──────────────────────────────────────────────────────────

    def ensure_runtime_paths(self) -> None:
        """Create upload/results directories and verify PocketHunter exists.

        Mirrors the side effects v1 ``Config.validate()`` had. Called once
        at module import via ``config.py``'s facade.
        """
        if not self.POCKETHUNTER_DIR.exists():
            raise ValueError(
                f"PocketHunter directory not found: {self.POCKETHUNTER_DIR}"
            )
        if not self.POCKETHUNTER_CLI.exists():
            raise ValueError(
                f"PocketHunter CLI not found: {self.POCKETHUNTER_CLI}"
            )
        self.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self.RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def _load_settings() -> Settings:
    """Construct the settings singleton.

    Kept as a function so tests can clear-and-rebuild via ``monkeypatch``
    and ``Settings()`` directly.
    """
    return Settings()  # type: ignore[call-arg]  # required fields come from env


# Module-level singleton — created on first import.
settings: Settings = _load_settings()
