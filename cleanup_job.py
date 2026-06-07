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


@celery_app.task
def enforce_session_disk_quotas_task():
    """Phase C C5: prune oldest jobs for sessions over PER_SESSION_DISK_QUOTA_MB.

    Walks every non-expired session, computes its on-disk usage, and if
    over the quota deletes the oldest result + upload directories until
    usage drops below the cap. Job rows stay (so the UI still shows the
    history); only their on-disk artefacts are removed — same contract
    ``ResourceManager.cleanup_old_jobs`` has for time-based pruning.

    Returns a per-session report so beat logs make the action visible.
    """
    import shutil

    from config import Config
    from db.jobs import find_by_session
    from db.session import get_db
    from db.sessions import disk_usage_mb
    from db.models import Session as SessionRow
    from sqlalchemy import select

    quota = Config.PER_SESSION_DISK_QUOTA_MB
    logger.info("session-quota enforcement starting (quota=%d MB)", quota)

    pruned = {}
    try:
        with get_db() as db:
            rows = db.scalars(
                select(SessionRow).where(SessionRow.expired_at.is_(None))
            ).all()
            for s in rows:
                used = disk_usage_mb(s.id, db=db)
                if used <= quota:
                    continue
                jobs = find_by_session(s.id, db=db)
                # Oldest first — find_by_session sorts newest-first, reverse it.
                victims = list(reversed(jobs))
                removed = []
                for job in victims:
                    if used <= quota:
                        break
                    if not job.legacy_id:
                        continue
                    rdir = Config.RESULTS_DIR / job.legacy_id
                    udir = Config.UPLOAD_DIR / job.legacy_id
                    if rdir.exists():
                        shutil.rmtree(rdir, ignore_errors=True)
                    if udir.exists():
                        shutil.rmtree(udir, ignore_errors=True)
                    removed.append(job.legacy_id)
                    used = disk_usage_mb(s.id, db=db)
                if removed:
                    pruned[str(s.id)] = {"final_mb": round(used, 1),
                                          "removed": removed}
                    logger.warning(
                        "session %s pruned %d job(s) to land at %.1f MB / %d MB cap",
                        s.short_code, len(removed), used, quota,
                    )
    except Exception as e:
        logger.error("session-quota enforcement failed: %s", e, exc_info=True)
        return {"status": "error", "error": str(e)}

    return {
        "status": "success",
        "quota_mb": quota,
        "sessions_pruned": len(pruned),
        "details": pruned,
    }


@celery_app.task
def cleanup_abandoned_sessions_task():
    """Delete sessions that have zero job rows and are older than
    ``SESSION_GRACE_MINUTES``.

    These sessions were created (Session row + audit log) but the user
    never dispatched any work — typically a tab opened on the landing
    page and closed without interaction. They:

    * shouldn't pollute the DB long-term, and
    * shouldn't count toward the per-IP daily limit
      (``check_session_create_rate_limit``).

    The cascade on ``Job.session_id`` (``ondelete=CASCADE``) cleans up
    any job rows that race in between the EXISTS check and the delete.
    ``AuditEvent.session_id`` is ``ondelete=SET NULL``, so audit rows
    survive for forensics with a NULL session reference.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import exists, not_, select

    from config import Config
    from db.models import Job, Session
    from db.sessions import get_db

    grace = timedelta(minutes=Config.SESSION_GRACE_MINUTES)
    cutoff = datetime.now(timezone.utc) - grace
    logger.info(
        "cleanup_abandoned_sessions: starting (cutoff=%s, grace=%dm)",
        cutoff.isoformat(), Config.SESSION_GRACE_MINUTES,
    )
    deleted = 0
    try:
        with get_db() as db:
            stmt = select(Session).where(
                Session.created_at < cutoff,
                not_(exists().where(Job.session_id == Session.id)),
            )
            rows = list(db.scalars(stmt).all())
            for row in rows:
                db.delete(row)
                deleted += 1
        logger.info(
            "cleanup_abandoned_sessions: deleted %d empty session(s)", deleted,
        )
    except Exception as e:
        logger.error(
            "cleanup_abandoned_sessions failed: %s", e, exc_info=True,
        )
        return {"status": "error", "error": str(e), "deleted": deleted}
    return {"status": "success", "deleted": deleted}


if __name__ == '__main__':
    # Test cleanup task
    print("Testing cleanup task...")
    result = cleanup_old_jobs_task()
    print(f"Result: {result}")

    print("\nTesting disk usage check...")
    usage = check_disk_usage_task()
    print(f"Usage: {usage}")
