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

from pydantic import AliasChoices, Field, field_validator
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
    # Renamed from MAX_DISK_USAGE_GB (defect D of the docs-driven fix pass):
    # the value only sets the threshold at which check_disk_usage_task /
    # health.py *report* disk usage as high (warning/critical log lines) —
    # nothing refuses an upload or prunes anything on it, so "MAX_..." was
    # misleading. The alias keeps reading the old env var name so a
    # deployed host's existing .env needs no edit.
    DISK_USAGE_WARN_GB: int = Field(
        default=100,
        validation_alias=AliasChoices("DISK_USAGE_WARN_GB", "MAX_DISK_USAGE_GB"),
    )
    MAX_DOCKING_PDBS: int = Field(default=20)
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

    # Maximum number of trajectory frames the viewer is willing to render.
    # Above this, browser memory + WebGL upload reliably fall over even
    # when the on-disk file is under MAX_VIEWER_BYTES. find_pockets refuses
    # the submission (ZIP mode) or fails the job after extract (trajectory
    # mode) with a clear "increase stride" hint.
    MAX_TRAJECTORY_FRAMES: int = Field(default=1000)
    # Hard cap on the raw byte size of the viewer file (multi-model PDB /
    # mmCIF) the worker is willing to build. Protects against pathological
    # inputs (e.g. solvated MD frames with 100k+ atoms each) that would
    # slug the browser regardless of frame count — i.e. complementary to
    # MAX_TRAJECTORY_FRAMES: the latter caps frame *count*, this caps
    # per-frame size × count. Default 1 GB; bump only if you've profiled
    # your inputs and your browser/GPU can handle the load.
    MAX_VIEWER_BYTES: int = Field(default=1024 * 1024 * 1024)  # 1 GB
    # Cap on how many models actually land in ``viewer.pdb`` — independent
    # of MAX_TRAJECTORY_FRAMES (which caps the *extracted* count that
    # pocket detection runs on). A uniform stride is applied at
    # ``convert_pdb_dir_to_viewer`` time; the per-frame PDBs that didn't
    # make the cut are still on disk and load on demand when a user
    # picks them via a pocket / cluster rep / docking receptor row.
    MAX_VIEWER_LOADED_FRAMES: int = Field(default=200)

    # ── Phase C: hardened worker pools (C1 scaffolding) ───────────────────
    # The orchestrator (C2+) reads these to size + spawn worker containers.
    # C1 only adds the knobs; the existing celery-worker / celery-docking-
    # worker services keep running off WORKER_CONCURRENCY / DOCKING_CONCURRENCY
    # for one release of back-compat.
    FAST_POOL_SIZE: int = Field(default=6)         # find_pockets / cluster / pipeline
    DOCKING_POOL_SIZE: int = Field(default=3)      # docking
    WORKER_CPU_LIMIT: int = Field(default=6)       # per-worker --cpus
    WORKER_MEMORY_LIMIT: str = Field(default="12g")  # per-worker --memory
    WORKER_JOBS_BEFORE_RECYCLE: int = Field(default=10)
    # Seconds to wait for a worker to finish its in-flight task on recycle
    # before force-killing. SIGTERM triggers celery's warm shutdown — the
    # worker stops accepting new tasks and exits after the current one
    # finishes. Defaults to DOCKING_TIMEOUT (2 h) so a docking pair never
    # gets cut short by recycle. Lower it only if you're confident your
    # tasks won't exceed the drain window.
    WORKER_DRAIN_TIMEOUT: int = Field(default=7200)
    WORKER_IMAGE_TAG: str = Field(default="pockethunter-worker:latest")  # canonical tag (compose builds it)
    ORCHESTRATOR_BACKEND: str = Field(default="docker_sdk")  # docker_sdk | k8s (stub)
    # Per-session caps to keep one user from monopolising a pool.
    PER_SESSION_DISK_QUOTA_MB: int = Field(default=5000)
    MAX_CONCURRENT_FAST_JOBS: int = Field(default=60)
    MAX_CONCURRENT_DOCKING_JOBS: int = Field(default=30)
    MAX_CONCURRENT_FAST_PER_SESSION: int = Field(default=2)
    MAX_CONCURRENT_DOCKING_PER_SESSION: int = Field(default=1)
    # Per-IP daily cap on COMMITTED sessions — i.e. sessions where the
    # user actually dispatched at least one job (find_pockets / cluster /
    # docking). Sessions that were created but never had any job rows
    # are forgiven AND auto-deleted by the ``cleanup_abandoned_sessions``
    # beat task after ``SESSION_GRACE_MINUTES`` so a casual visitor who
    # opens + closes a tab doesn't burn their quota.
    MAX_SESSIONS_PER_IP_PER_DAY: int = Field(default=20)
    # How long a freshly-created session stays "provisional" — counted
    # toward the daily limit even if no job has been dispatched yet.
    # After this, sessions with zero jobs are deleted by the cleanup
    # task and stop counting. 15 min is long enough to set up a
    # trajectory upload, short enough to forgive abandoned tabs.
    SESSION_GRACE_MINUTES: int = Field(default=15)

    # ── Phase C: orchestrator runtime knobs (C2 scaffolding) ──────────────
    # Master toggle. With USE_ORCHESTRATOR=false the orchestrator boots its
    # HTTP API and reconcile loop but spawns *zero* worker containers, so
    # the legacy celery-worker / celery-docking-worker services in
    # docker-compose.yml stay in charge of real jobs. Flip to true to hand
    # the pools over to the orchestrator (C3 cut-over).
    USE_ORCHESTRATOR: bool = Field(default=False)
    # Docker SDK reaches the daemon through the socket proxy at this URL.
    # On the production single-box deployment that's the in-network
    # tcp://docker-proxy:2375; local tests can point at unix:///var/run/docker.sock.
    DOCKER_PROXY_URL: str = Field(default="tcp://docker-proxy:2375")
    # HTTP port the orchestrator's /pool/status + /healthz endpoints listen on.
    ORCHESTRATOR_HTTP_PORT: int = Field(default=9000)
    # How often the reconcile loop fires (recycle-trigger + orphan reap).
    ORCHESTRATOR_TICK_SECONDS: int = Field(default=30)
    # Internal Docker network workers attach to (no public egress).
    WORKER_NETWORK_NAME: str = Field(default="pockethunter_internal")

    # ── Demo data (B3.1 + post-launch knob) ──────────────────────────────
    # Host directory containing ``trajectory.xtc`` + ``topology.gro`` for
    # the landing-page "Try with example trajectory" button. Empty string
    # OR a non-existent path hides the button entirely — handy for ops
    # who don't want to ship a demo. Filenames inside the dir are fixed
    # (operators rename their files to match if relocating).
    EXAMPLE_TRAJECTORY_DIR: str = Field(
        default=str(_DEFAULT_BASE_DIR / "examples" / "trypsin")
    )

    # ── Cloudflare Turnstile (session-create CAPTCHA, Phase D) ───────────
    # Second-line defence against scripted abuse on top of the per-IP daily
    # session-create cap. Disabled by default so local-dev doesn't need a
    # CF account; flip TURNSTILE_ENABLED=true on the production deploy and
    # set both keys. Keys come from cloudflare.com/products/turnstile.
    TURNSTILE_ENABLED: bool = Field(default=False)
    TURNSTILE_SITE_KEY: str = Field(default="")
    TURNSTILE_SECRET_KEY: str = Field(default="")

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
                     "DISK_USAGE_WARN_GB", "WORKER_CONCURRENCY",
                     "DOCKING_CONCURRENCY", "P2RANK_THREADS",
                     "DOCKING_EXHAUSTIVENESS", "DOCKING_MAX_PAIRS",
                     "MAX_DOWNLOAD_ZIP_SIZE", "MAX_TRAJECTORY_FRAMES",
                     "MAX_VIEWER_BYTES", "MAX_VIEWER_LOADED_FRAMES",
                     # Phase C C1 — pool sizing + abuse limits
                     "FAST_POOL_SIZE", "DOCKING_POOL_SIZE",
                     "WORKER_CPU_LIMIT", "WORKER_JOBS_BEFORE_RECYCLE",
                     "PER_SESSION_DISK_QUOTA_MB",
                     "MAX_CONCURRENT_FAST_JOBS", "MAX_CONCURRENT_DOCKING_JOBS",
                     "MAX_CONCURRENT_FAST_PER_SESSION",
                     "MAX_CONCURRENT_DOCKING_PER_SESSION",
                     "SESSION_GRACE_MINUTES",
                     "MAX_SESSIONS_PER_IP_PER_DAY",
                     "ORCHESTRATOR_HTTP_PORT", "ORCHESTRATOR_TICK_SECONDS")
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
