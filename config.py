"""
Centralized configuration management for PocketHunter-Suite.

This module provides a single source of truth for all application configuration,
including paths, limits, and environment-specific settings.
"""

import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class ConfigurationError(Exception):
    """Raised when configuration is invalid or missing required values."""
    pass


class Config:
    """Application configuration with validation and defaults."""

    # ========================================
    # Base Directories (absolute paths)
    # ========================================
    BASE_DIR = Path(__file__).parent.resolve()
    UPLOAD_DIR = Path(os.getenv('UPLOAD_DIR', BASE_DIR / 'uploads')).resolve()
    RESULTS_DIR = Path(os.getenv('RESULTS_DIR', BASE_DIR / 'results')).resolve()

    # ========================================
    # PocketHunter CLI
    # ========================================
    POCKETHUNTER_DIR = BASE_DIR / 'PocketHunter'
    POCKETHUNTER_CLI = POCKETHUNTER_DIR / 'pockethunter.py'

    # ========================================
    # External Tools
    # ========================================
    P2RANK_PATH = os.getenv('P2RANK_PATH', 'prank')  # Will use PATH if not specified
    SMINA_PATH = os.getenv('SMINA_PATH', 'smina')    # Will use PATH if not specified

    # ========================================
    # Celery/Redis Configuration
    # ========================================
    CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
    CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')

    # ========================================
    # File Upload Limits (bytes)
    # ========================================
    MAX_UPLOAD_SIZE = int(os.getenv('MAX_UPLOAD_SIZE', 524288000))  # 500 MB default
    MAX_ZIP_SIZE = int(os.getenv('MAX_ZIP_SIZE', 1073741824))       # 1 GB default

    # ========================================
    # Security
    # ========================================
    ALLOWED_UPLOAD_EXTENSIONS = {
        '.xtc',    # Trajectory files
        '.pdb',    # Protein structure
        '.gro',    # Gromacs structure
        '.csv',    # Data files
        '.zip',    # Compressed archives
        '.sdf',    # Structure-data files
        '.pdbqt',  # AutoDock format
    }

    ALLOWED_MIME_TYPES = {
        'application/octet-stream',  # .xtc, .gro
        'chemical/x-pdb',            # .pdb
        'text/csv',                  # .csv
        'application/zip',           # .zip
        'chemical/x-mdl-sdfile',     # .sdf
        'text/plain',                # .pdbqt
    }

    # ========================================
    # Resource Management
    # ========================================
    CLEANUP_AFTER_DAYS = int(os.getenv('CLEANUP_AFTER_DAYS', 30))
    MAX_DISK_USAGE_GB = int(os.getenv('MAX_DISK_USAGE_GB', 100))

    # ========================================
    # Logging Configuration
    # ========================================
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE = os.getenv('LOG_FILE', str(BASE_DIR / 'pockethunter-suite.log'))

    # ========================================
    # Class Methods
    # ========================================

    @classmethod
    def validate(cls) -> None:
        """
        Validate configuration and raise ConfigurationError if invalid.

        This method checks:
        - Required directories exist or can be created
        - Required files (PocketHunter CLI) exist
        - URLs are properly formatted

        Raises:
            ConfigurationError: If configuration is invalid
        """
        errors = []

        # Check PocketHunter directory exists
        if not cls.POCKETHUNTER_DIR.exists():
            errors.append(
                f"PocketHunter directory not found: {cls.POCKETHUNTER_DIR}\n"
                f"Expected to find PocketHunter as a subdirectory of {cls.BASE_DIR}"
            )

        # Check PocketHunter CLI exists
        if not cls.POCKETHUNTER_CLI.exists():
            errors.append(
                f"PocketHunter CLI not found: {cls.POCKETHUNTER_CLI}\n"
                f"Expected to find pockethunter.py in {cls.POCKETHUNTER_DIR}"
            )

        # Create upload/results directories if they don't exist
        try:
            cls.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            cls.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            errors.append(f"Failed to create required directories: {e}")

        # Validate Redis URL format
        if not cls.CELERY_BROKER_URL.startswith('redis://'):
            errors.append(
                f"Invalid CELERY_BROKER_URL: {cls.CELERY_BROKER_URL}\n"
                f"Expected format: redis://host:port/db"
            )

        # Validate numeric limits
        if cls.MAX_UPLOAD_SIZE <= 0:
            errors.append(f"MAX_UPLOAD_SIZE must be positive, got: {cls.MAX_UPLOAD_SIZE}")

        if cls.MAX_ZIP_SIZE <= 0:
            errors.append(f"MAX_ZIP_SIZE must be positive, got: {cls.MAX_ZIP_SIZE}")

        if cls.CLEANUP_AFTER_DAYS <= 0:
            errors.append(f"CLEANUP_AFTER_DAYS must be positive, got: {cls.CLEANUP_AFTER_DAYS}")

        if cls.MAX_DISK_USAGE_GB <= 0:
            errors.append(f"MAX_DISK_USAGE_GB must be positive, got: {cls.MAX_DISK_USAGE_GB}")

        # Validate LOG_LEVEL
        valid_log_levels = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}
        if cls.LOG_LEVEL.upper() not in valid_log_levels:
            errors.append(
                f"Invalid LOG_LEVEL: {cls.LOG_LEVEL}\n"
                f"Valid options: {', '.join(valid_log_levels)}"
            )

        # If there are errors, raise them all at once
        if errors:
            raise ConfigurationError(
                "Configuration validation failed:\n\n" +
                "\n\n".join(f"  • {error}" for error in errors)
            )

    @classmethod
    def get_upload_path(cls, job_id: str, filename: str) -> Path:
        """
        Generate secure upload path for a file.

        This method:
        - Sanitizes the filename to prevent path traversal
        - Creates the job directory if it doesn't exist
        - Returns an absolute path under UPLOAD_DIR

        Args:
            job_id: Unique job identifier
            filename: Original filename (will be sanitized)

        Returns:
            Absolute path where file should be saved

        Example:
            >>> Config.get_upload_path("abc123", "trajectory.xtc")
            PosixPath('/app/uploads/abc123/trajectory.xtc')
        """
        # Sanitize filename to remove any directory components
        safe_filename = Path(filename).name

        # Create job directory
        job_dir = cls.UPLOAD_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        return job_dir / safe_filename

    @classmethod
    def get_results_path(cls, job_id: str) -> Path:
        """
        Generate results directory path for a job.

        Args:
            job_id: Unique job identifier

        Returns:
            Absolute path to results directory

        Example:
            >>> Config.get_results_path("abc123")
            PosixPath('/app/results/abc123')
        """
        results_path = cls.RESULTS_DIR / job_id
        results_path.mkdir(parents=True, exist_ok=True)
        return results_path

    @classmethod
    def get_status_file(cls, job_id: str) -> Path:
        """
        Get path to job status JSON file.

        Args:
            job_id: Unique job identifier

        Returns:
            Path to status JSON file

        Example:
            >>> Config.get_status_file("abc123")
            PosixPath('/app/results/abc123_status.json')
        """
        return cls.RESULTS_DIR / f"{job_id}_status.json"

    @classmethod
    def print_config(cls) -> None:
        """Print current configuration (useful for debugging)."""
        print("=" * 60)
        print("PocketHunter-Suite Configuration")
        print("=" * 60)
        print(f"BASE_DIR:              {cls.BASE_DIR}")
        print(f"UPLOAD_DIR:            {cls.UPLOAD_DIR}")
        print(f"RESULTS_DIR:           {cls.RESULTS_DIR}")
        print(f"POCKETHUNTER_CLI:      {cls.POCKETHUNTER_CLI}")
        print(f"CELERY_BROKER_URL:     {cls.CELERY_BROKER_URL}")
        print(f"MAX_UPLOAD_SIZE:       {cls.MAX_UPLOAD_SIZE / (1024**2):.1f} MB")
        print(f"MAX_ZIP_SIZE:          {cls.MAX_ZIP_SIZE / (1024**3):.1f} GB")
        print(f"CLEANUP_AFTER_DAYS:    {cls.CLEANUP_AFTER_DAYS} days")
        print(f"MAX_DISK_USAGE_GB:     {cls.MAX_DISK_USAGE_GB} GB")
        print(f"LOG_LEVEL:             {cls.LOG_LEVEL}")
        print(f"LOG_FILE:              {cls.LOG_FILE}")
        print("=" * 60)


# Validate configuration on module import
# This ensures errors are caught early during startup
try:
    Config.validate()
except ConfigurationError as e:
    print(f"\n{'='*60}")
    print("CONFIGURATION ERROR")
    print(f"{'='*60}")
    print(str(e))
    print(f"{'='*60}\n")
    raise


# For debugging: uncomment to print config on import
# Config.print_config()
