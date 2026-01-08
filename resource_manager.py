"""
Resource management utilities for disk space and cleanup operations.

This module provides:
- Disk space monitoring
- Automatic cleanup of old job directories
- Resource usage reporting
"""

import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple, Dict, Any
from config import Config
from logging_config import setup_logging

logger = setup_logging(__name__)


class ResourceManager:
    """Manages disk space, cleanup operations, and resource monitoring."""

    @staticmethod
    def get_directory_size(path: Path) -> int:
        """
        Calculate total size of directory in bytes.

        Recursively sums the size of all files in the directory.

        Args:
            path: Directory path to measure

        Returns:
            Total size in bytes

        Example:
            >>> size = ResourceManager.get_directory_size(Path("/app/uploads"))
            >>> print(f"{size / (1024**3):.2f} GB")
            1.23 GB
        """
        total = 0
        try:
            for entry in path.rglob('*'):
                if entry.is_file():
                    try:
                        total += entry.stat().st_size
                    except (OSError, PermissionError) as e:
                        logger.warning(f"Cannot stat file {entry}: {e}")
        except Exception as e:
            logger.error(f"Error calculating directory size for {path}: {e}")

        return total

    @staticmethod
    def get_directory_info(path: Path) -> Dict[str, Any]:
        """
        Get detailed information about a directory.

        Args:
            path: Directory path

        Returns:
            Dictionary with size, file count, and last modified time

        Example:
            >>> info = ResourceManager.get_directory_info(Path("/app/uploads/job123"))
            >>> print(info)
            {'size_bytes': 1234567, 'file_count': 42, 'last_modified': datetime(...)}
        """
        if not path.exists():
            return {
                'size_bytes': 0,
                'file_count': 0,
                'last_modified': None,
                'exists': False
            }

        size = ResourceManager.get_directory_size(path)
        file_count = sum(1 for _ in path.rglob('*') if _.is_file())

        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
        except (OSError, PermissionError):
            mtime = None

        return {
            'size_bytes': size,
            'file_count': file_count,
            'last_modified': mtime,
            'exists': True
        }

    @staticmethod
    def check_disk_usage() -> Tuple[int, int, float]:
        """
        Check current disk usage for uploads and results directories.

        Returns:
            Tuple of (used_bytes, limit_bytes, usage_percentage)

        Example:
            >>> used, limit, pct = ResourceManager.check_disk_usage()
            >>> print(f"Using {pct:.1f}% of allowed space")
            Using 45.2% of allowed space
        """
        uploads_size = ResourceManager.get_directory_size(Config.UPLOAD_DIR)
        results_size = ResourceManager.get_directory_size(Config.RESULTS_DIR)
        total_used = uploads_size + results_size

        limit_bytes = Config.MAX_DISK_USAGE_GB * (1024 ** 3)
        usage_pct = (total_used / limit_bytes) * 100 if limit_bytes > 0 else 0

        logger.debug(
            f"Disk usage: {total_used / (1024**3):.2f} GB / "
            f"{Config.MAX_DISK_USAGE_GB} GB ({usage_pct:.1f}%)"
        )

        return total_used, limit_bytes, usage_pct

    @staticmethod
    def get_usage_report() -> Dict[str, Any]:
        """
        Get detailed usage report for all directories.

        Returns:
            Dictionary with usage statistics

        Example:
            >>> report = ResourceManager.get_usage_report()
            >>> print(report)
            {
                'uploads': {'size_gb': 1.2, 'file_count': 150},
                'results': {'size_gb': 2.3, 'file_count': 300},
                'total_size_gb': 3.5,
                'limit_gb': 100,
                'usage_pct': 3.5
            }
        """
        uploads_info = ResourceManager.get_directory_info(Config.UPLOAD_DIR)
        results_info = ResourceManager.get_directory_info(Config.RESULTS_DIR)

        total_bytes = uploads_info['size_bytes'] + results_info['size_bytes']
        limit_bytes = Config.MAX_DISK_USAGE_GB * (1024 ** 3)
        usage_pct = (total_bytes / limit_bytes) * 100 if limit_bytes > 0 else 0

        return {
            'uploads': {
                'size_gb': uploads_info['size_bytes'] / (1024 ** 3),
                'size_mb': uploads_info['size_bytes'] / (1024 ** 2),
                'file_count': uploads_info['file_count']
            },
            'results': {
                'size_gb': results_info['size_bytes'] / (1024 ** 3),
                'size_mb': results_info['size_bytes'] / (1024 ** 2),
                'file_count': results_info['file_count']
            },
            'total_size_gb': total_bytes / (1024 ** 3),
            'total_size_mb': total_bytes / (1024 ** 2),
            'limit_gb': Config.MAX_DISK_USAGE_GB,
            'usage_pct': round(usage_pct, 2)
        }

    @staticmethod
    def cleanup_old_jobs(dry_run: bool = False) -> List[str]:
        """
        Remove job directories older than CLEANUP_AFTER_DAYS.

        Deletes both upload and result directories for jobs that haven't
        been modified within the configured retention period.

        Args:
            dry_run: If True, only report what would be deleted without deleting

        Returns:
            List of deleted job IDs

        Example:
            >>> # Preview what would be deleted
            >>> jobs = ResourceManager.cleanup_old_jobs(dry_run=True)
            >>> print(f"Would delete {len(jobs)} jobs")

            >>> # Actually delete old jobs
            >>> jobs = ResourceManager.cleanup_old_jobs(dry_run=False)
            >>> print(f"Deleted {len(jobs)} jobs")
        """
        cutoff_date = datetime.now() - timedelta(days=Config.CLEANUP_AFTER_DAYS)
        deleted_jobs = []

        logger.info(
            f"{'[DRY RUN] ' if dry_run else ''}Starting cleanup of jobs older than "
            f"{Config.CLEANUP_AFTER_DAYS} days (before {cutoff_date.isoformat()})"
        )

        for directory in [Config.UPLOAD_DIR, Config.RESULTS_DIR]:
            if not directory.exists():
                logger.warning(f"Directory does not exist: {directory}")
                continue

            dir_type = "uploads" if directory == Config.UPLOAD_DIR else "results"

            for job_dir in directory.iterdir():
                if not job_dir.is_dir():
                    continue

                # Skip special directories
                if job_dir.name.startswith('.') or job_dir.name in ('ligands_temp', '__pycache__'):
                    continue

                try:
                    # Check modification time
                    mtime = datetime.fromtimestamp(job_dir.stat().st_mtime)

                    if mtime < cutoff_date:
                        size_mb = ResourceManager.get_directory_size(job_dir) / (1024 ** 2)

                        if dry_run:
                            logger.info(
                                f"[DRY RUN] Would delete {dir_type}/{job_dir.name} "
                                f"(modified: {mtime.isoformat()}, size: {size_mb:.1f} MB)"
                            )
                        else:
                            shutil.rmtree(job_dir)
                            logger.info(
                                f"Deleted {dir_type}/{job_dir.name} "
                                f"(modified: {mtime.isoformat()}, size: {size_mb:.1f} MB)"
                            )

                        # Add to deleted list only once per job
                        if job_dir.name not in deleted_jobs:
                            deleted_jobs.append(job_dir.name)

                except Exception as e:
                    logger.error(f"Failed to process {job_dir}: {e}")

        logger.info(
            f"{'[DRY RUN] ' if dry_run else ''}Cleanup complete: "
            f"{len(deleted_jobs)} job(s) {'would be ' if dry_run else ''}deleted"
        )

        return deleted_jobs

    @staticmethod
    def cleanup_temp_files() -> int:
        """
        Clean up temporary files in uploads directory.

        Removes files in temporary directories like ligands_temp.

        Returns:
            Number of files deleted

        Example:
            >>> count = ResourceManager.cleanup_temp_files()
            >>> print(f"Deleted {count} temporary files")
        """
        temp_dirs = [
            Config.UPLOAD_DIR / 'ligands_temp',
        ]

        deleted_count = 0

        for temp_dir in temp_dirs:
            if not temp_dir.exists():
                continue

            try:
                size_before = ResourceManager.get_directory_size(temp_dir)
                file_count = sum(1 for _ in temp_dir.rglob('*') if _.is_file())

                shutil.rmtree(temp_dir)
                temp_dir.mkdir(parents=True, exist_ok=True)

                logger.info(
                    f"Cleaned temp directory {temp_dir.name}: "
                    f"{file_count} files, {size_before / (1024**2):.1f} MB"
                )

                deleted_count += file_count

            except Exception as e:
                logger.error(f"Failed to clean temp directory {temp_dir}: {e}")

        return deleted_count

    @staticmethod
    def check_space_available(required_bytes: int) -> bool:
        """
        Check if sufficient disk space is available for an operation.

        Args:
            required_bytes: Required space in bytes

        Returns:
            True if space is available

        Example:
            >>> # Check if we can store a 500 MB file
            >>> if ResourceManager.check_space_available(500 * 1024 * 1024):
            ...     print("Space available")
            ... else:
            ...     print("Not enough space")
        """
        used, limit, usage_pct = ResourceManager.check_disk_usage()
        available = limit - used

        if available >= required_bytes:
            return True

        logger.warning(
            f"Insufficient space: need {required_bytes / (1024**2):.1f} MB, "
            f"have {available / (1024**2):.1f} MB available"
        )
        return False

    @staticmethod
    def get_oldest_jobs(count: int = 10) -> List[Dict[str, Any]]:
        """
        Get information about the oldest job directories.

        Useful for identifying jobs that should be cleaned up.

        Args:
            count: Number of oldest jobs to return

        Returns:
            List of job info dictionaries sorted by age (oldest first)

        Example:
            >>> old_jobs = ResourceManager.get_oldest_jobs(5)
            >>> for job in old_jobs:
            ...     print(f"{job['job_id']}: {job['age_days']} days old")
        """
        jobs = []

        for directory in [Config.UPLOAD_DIR, Config.RESULTS_DIR]:
            if not directory.exists():
                continue

            for job_dir in directory.iterdir():
                if not job_dir.is_dir() or job_dir.name.startswith('.'):
                    continue

                try:
                    info = ResourceManager.get_directory_info(job_dir)
                    if info['last_modified']:
                        age = datetime.now() - info['last_modified']
                        jobs.append({
                            'job_id': job_dir.name,
                            'last_modified': info['last_modified'],
                            'age_days': age.days,
                            'size_mb': info['size_bytes'] / (1024 ** 2),
                            'file_count': info['file_count']
                        })
                except Exception as e:
                    logger.error(f"Error getting info for {job_dir}: {e}")

        # Sort by age (oldest first) and return top N
        jobs.sort(key=lambda x: x['last_modified'])
        return jobs[:count]


if __name__ == '__main__':
    # Test resource manager
    print("Testing Resource Manager...")
    print("=" * 60)

    # Get usage report
    report = ResourceManager.get_usage_report()
    print("\nUsage Report:")
    print(f"  Uploads: {report['uploads']['size_gb']:.2f} GB ({report['uploads']['file_count']} files)")
    print(f"  Results: {report['results']['size_gb']:.2f} GB ({report['results']['file_count']} files)")
    print(f"  Total: {report['total_size_gb']:.2f} GB / {report['limit_gb']} GB ({report['usage_pct']:.1f}%)")

    # Check disk usage
    used, limit, pct = ResourceManager.check_disk_usage()
    print(f"\nDisk Usage: {pct:.1f}%")

    # Dry run cleanup
    print(f"\nRunning cleanup dry run...")
    jobs_to_delete = ResourceManager.cleanup_old_jobs(dry_run=True)
    print(f"Found {len(jobs_to_delete)} jobs that could be deleted")

    # Get oldest jobs
    print(f"\nOldest 5 jobs:")
    for job in ResourceManager.get_oldest_jobs(5):
        print(f"  {job['job_id']}: {job['age_days']} days old, {job['size_mb']:.1f} MB")

    print("\n✅ Resource Manager tests complete!")
