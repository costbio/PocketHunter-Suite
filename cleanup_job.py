"""
Periodic cleanup task for PocketHunter-Suite.

This module provides a Celery Beat task that automatically cleans up
old job directories and temporary files to prevent unbounded disk growth.
"""

from celery import Celery
from celery.schedules import crontab
from celery_app import celery_app
from resource_manager import ResourceManager
from logging_config import setup_logging

logger = setup_logging(__name__)


@celery_app.task
def cleanup_old_jobs_task():
    """
    Periodic task to clean up old job directories.

    Removes job directories (both uploads and results) that are older than
    CLEANUP_AFTER_DAYS configured in the environment.

    Returns:
        Dictionary with cleanup results:
        - status: 'success' or 'error'
        - deleted_count: Number of jobs deleted
        - deleted_jobs: List of deleted job IDs
        - error: Error message (if status is 'error')

    Example:
        >>> # Manually trigger cleanup
        >>> result = cleanup_old_jobs_task.delay()
        >>> print(result.get())
        {'status': 'success', 'deleted_count': 5, 'deleted_jobs': ['job1', 'job2', ...]}
    """
    logger.info("Starting periodic cleanup task")

    try:
        # Perform cleanup (not a dry run)
        deleted_jobs = ResourceManager.cleanup_old_jobs(dry_run=False)

        # Also clean up temporary files
        temp_files_deleted = ResourceManager.cleanup_temp_files()

        # Get current disk usage after cleanup
        used, limit, usage_pct = ResourceManager.check_disk_usage()

        result = {
            'status': 'success',
            'deleted_count': len(deleted_jobs),
            'deleted_jobs': deleted_jobs,
            'temp_files_deleted': temp_files_deleted,
            'disk_usage_pct': round(usage_pct, 2),
            'disk_used_gb': round(used / (1024 ** 3), 2)
        }

        logger.info(
            f"Cleanup completed: {len(deleted_jobs)} jobs deleted, "
            f"{temp_files_deleted} temp files removed, "
            f"disk usage: {usage_pct:.1f}%"
        )

        return result

    except Exception as e:
        logger.error(f"Cleanup task failed: {e}", exc_info=True)
        return {
            'status': 'error',
            'error': str(e)
        }


@celery_app.task
def check_disk_usage_task():
    """
    Periodic task to check disk usage and log warnings if approaching limits.

    This task runs more frequently than cleanup to provide early warning
    if disk usage is getting high.

    Returns:
        Dictionary with disk usage information
    """
    try:
        report = ResourceManager.get_usage_report()

        # Log warnings if usage is high
        usage_pct = report['usage_pct']
        if usage_pct > 90:
            logger.critical(
                f"CRITICAL: Disk usage at {usage_pct:.1f}% "
                f"({report['total_size_gb']:.2f} GB / {report['limit_gb']} GB)"
            )
        elif usage_pct > 75:
            logger.warning(
                f"WARNING: Disk usage at {usage_pct:.1f}% "
                f"({report['total_size_gb']:.2f} GB / {report['limit_gb']} GB)"
            )
        else:
            logger.info(
                f"Disk usage: {usage_pct:.1f}% "
                f"({report['total_size_gb']:.2f} GB / {report['limit_gb']} GB)"
            )

        return report

    except Exception as e:
        logger.error(f"Disk usage check failed: {e}", exc_info=True)
        return {'status': 'error', 'error': str(e)}


if __name__ == '__main__':
    # Test cleanup task
    print("Testing cleanup task...")
    result = cleanup_old_jobs_task()
    print(f"Result: {result}")

    print("\nTesting disk usage check...")
    usage = check_disk_usage_task()
    print(f"Usage: {usage}")
