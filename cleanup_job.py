"""
Periodic cleanup task for PocketHunter-Suite.

This module provides a Celery Beat task that automatically cleans up
old job directories and temporary files to prevent unbounded disk growth.
"""

from celery import Celery
from celery.schedules import crontab
from celery_app import celery_app
from config import Config
from resource_manager import ResourceManager
from logging_config import setup_logging
import tasks

logger = setup_logging(__name__)


# ── Stale-job reaper ───────────────────────────────────────────────────────
#
# Production defect: a worker can die (OOM kill, orchestrator recycle, host
# reboot) between writing status='running' and finishing. task_acks_late +
# task_reject_on_worker_lost (celery_app.py) are meant to redeliver such a
# task, but when that doesn't happen — as observed live — no terminal
# status is ever recorded, and the Job row sits in 'submitted' / 'queued' /
# 'running' forever. Two symptoms follow:
#   * pool_load.compute_pool_load counts it via db.jobs.in_flight_count, so
#     the masthead chip reports phantom busy workers on an idle service.
#   * panels/_shared.assert_submit_allowed uses the SAME in_flight_count for
#     its per-session concurrency cap (MAX_CONCURRENT_FAST_PER_SESSION=2),
#     so a phantom row permanently eats a slot.
# None of the other four beat tasks in this file reap it: cleanup_old_jobs
# only prunes on-disk directories, enforce_session_disk_quotas only prunes
# disk usage, and cleanup_abandoned_sessions_task explicitly skips any
# session that HAS a job row — the very thing a phantom Job protects it
# with.
#
# The cutoff below is "how long could this job possibly still be
# legitimately alive", derived from the *actual* hard time limits tasks.py
# (and the docking task's own Celery decorator) enforce — not a guessed
# number:
#
#   find_pockets  EXTRACT_TIMEOUT + DETECT_TIMEOUT (1800 + 3600 = 5400s).
#                 tasks.run_find_pockets_task has no bind time_limit of its
#                 own; in trajectory mode it runs the extract stage then the
#                 detect stage back-to-back inside ONE task attempt, and
#                 each stage is killed by tasks._run_stage's own
#                 subprocess.kill() once elapsed time exceeds that stage's
#                 timeout. The sum is the longest a single attempt can
#                 legitimately run.
#   cluster       2 * CLUSTER_TIMEOUT (2 * 1800 = 3600s).
#                 tasks.run_cluster_pockets_task kills its subprocess after
#                 CLUSTER_TIMEOUT via its own poll loop, but on the known
#                 DBSCAN-hierarchical "empty distance matrix" crash it
#                 re-runs the whole clustering subprocess once more, again
#                 bounded by CLUSTER_TIMEOUT — so twice that is the longest
#                 a single attempt can legitimately run.
#   docking       Config.DOCKING_TIMEOUT + 300 (default 7200 + 300 = 7500s).
#                 Not derived from internal polling like the other two —
#                 it IS the hard bound: tasks.run_docking_task's own
#                 @celery_app.task decorator sets
#                 soft_time_limit=Config.DOCKING_TIMEOUT and
#                 time_limit=Config.DOCKING_TIMEOUT + 300, so Celery itself
#                 SIGKILLs the worker process at that point — nothing can
#                 legitimately run longer.
#
# On top of each "worst possible single attempt" figure we add a flat
# safety margin to absorb ordinary scheduling noise -- the couple of
# seconds of slack in _run_stage's own 1s poll loop, plus the gap between
# a worker crashing and this beat task's own hourly tick -- without having
# to guess how large that noise really is. It's deliberately small
# relative to even the shortest cutoff (cluster's 3600s) so it can't mask
# a real zombie for long.
#
# What this margin does NOT cover -- and why this reaper only ever
# queries status == 'running': the cutoffs above bound how long a job can
# run once a worker has actually started it. They say nothing about how
# long a job may legitimately sit unclaimed in 'submitted' / 'queued'.
# That wait is governed by pool saturation, not by any task time limit,
# and this codebase deliberately oversubscribes both pools:
# FAST_POOL_SIZE=6 workers against MAX_CONCURRENT_FAST_JOBS=60, and
# DOCKING_POOL_SIZE=3 workers against MAX_CONCURRENT_DOCKING_JOBS=30
# (settings.py) -- a 10x ratio in both pools. Under a full backlog the
# last-in-line job can wait roughly (ratio - 1) x the pool's worst-case
# per-job runtime before a worker even looks at it -- for the fast pool
# that's up to ~9 x 5400s (~13.5h) using find_pockets' own worst case as
# the conservative per-job estimate, an order of magnitude past the
# find_pockets cutoff above. Applying the run-time cutoffs to
# 'submitted'/'queued' rows would misclassify healthy backlog as a dead
# worker -- telling a user their queued job failed, and inviting a
# resubmit, at exactly the moment the pool is already backlogged: the
# worst possible time. Safely bounding 'submitted'/'queued' staleness
# needs to consult Celery's own task state, the way
# panels/docking.py:_reattach_if_running already does for its
# PENDING-vs-lost check -- that's future work. This reaper narrows itself
# to the one thing it can prove from wall-clock age alone: a job a worker
# already claimed (status='running') that has run far longer than any
# task in this codebase is allowed to run.
_REAPER_SAFETY_MARGIN_SECONDS = 15 * 60  # 15 minutes

STALE_JOB_CUTOFF_SECONDS: dict = {
    "find_pockets": tasks.EXTRACT_TIMEOUT + tasks.DETECT_TIMEOUT
                     + _REAPER_SAFETY_MARGIN_SECONDS,
    "cluster": 2 * tasks.CLUSTER_TIMEOUT + _REAPER_SAFETY_MARGIN_SECONDS,
    "docking": Config.DOCKING_TIMEOUT + 300 + _REAPER_SAFETY_MARGIN_SECONDS,
}
# An unrecognized future job kind gets the most conservative (largest)
# cutoff rather than the shortest, so it can never be reaped too early.
_DEFAULT_STALE_CUTOFF_SECONDS = max(STALE_JOB_CUTOFF_SECONDS.values())


def _as_aware_utc(dt):
    """Treat a naive datetime as UTC.

    Postgres' ``DateTime(timezone=True)`` columns round-trip tz-aware, but
    SQLite (the test DB) drops tzinfo — same caveat panels/docking._is_stale
    already documents for this codebase's other staleness check.
    """
    from datetime import timezone as _timezone
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_timezone.utc)
    return dt


@celery_app.task
def reap_stale_jobs_task():
    """Mark 'running' Job rows abandoned by a dead worker as 'failed'.

    Finds every Job row with status == 'running' (deliberately narrower
    than ``db.jobs.in_flight_count``'s ``submitted`` / ``queued`` /
    ``running`` set — see the module comment above for why 'submitted' and
    'queued' are excluded) whose ``updated_at`` — the timestamp of its
    last real status transition, since intermediate Celery PROGRESS ticks
    never touch the DB row — is older than that job kind's
    ``STALE_JOB_CUTOFF_SECONDS``. Such a row cannot possibly still be
    legitimately running: see the module comment above for how each
    cutoff is derived from tasks.py's own hard time limits.

    Reaped rows are marked 'failed' through ``db.jobs.update_status`` (never
    raw SQL) with a structured ``error`` dict shaped like every other task
    failure in this codebase (``exc_type`` / ``exc_message`` / ``stage`` —
    see ``tasks._fail_job``), so ``failure_view.render_task_failure`` renders
    it exactly like any other failed job.

    Returns a dict with the reaped count so beat logs make the action
    visible.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select

    from db.jobs import update_status
    from db.models import Job
    from db.session import get_db

    now = datetime.now(timezone.utc)
    logger.info("reap_stale_jobs: starting (now=%s)", now.isoformat())

    reaped = 0
    try:
        with get_db() as db:
            stmt = select(Job).where(Job.status == "running")
            rows = list(db.scalars(stmt).all())
            for row in rows:
                cutoff = STALE_JOB_CUTOFF_SECONDS.get(
                    row.kind, _DEFAULT_STALE_CUTOFF_SECONDS,
                )
                age = (now - _as_aware_utc(row.updated_at)).total_seconds()
                if age <= cutoff:
                    continue

                error = {
                    "exc_type": "StaleJobReaped",
                    "exc_message": (
                        f"This {row.kind} job was still '{row.status}' after "
                        f"{age / 3600:.1f}h with no update from any worker — "
                        f"longer than a {row.kind} job can legitimately take "
                        f"({cutoff / 3600:.1f}h). The worker that picked it "
                        "up almost certainly died (killed, out of memory, "
                        "container recycle, or host restart) before it could "
                        "record success or failure, so no result was ever "
                        "produced. This job was automatically marked failed "
                        "by the stale-job reaper; resubmit if you still need "
                        "this run."
                    ),
                    "stage": "reaper",
                }
                update_status(row.id, "failed", error=error, db=db)
                reaped += 1
                logger.warning(
                    "reap_stale_jobs: reaped job %s (kind=%s, status=%s, "
                    "stuck %.0fs > cutoff %ds)",
                    row.id, row.kind, row.status, age, cutoff,
                )
        logger.info("reap_stale_jobs: reaped %d stale job(s)", reaped)
    except Exception as e:
        logger.error("reap_stale_jobs failed: %s", e, exc_info=True)
        return {"status": "error", "error": str(e), "reaped": reaped}
    return {"status": "success", "reaped": reaped}


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

    # Which job directories must survive regardless of age. Resolve this
    # BEFORE the sweep and bail out if it fails: cleanup_old_jobs treats an
    # empty set as "protect nothing", so a DB outage would otherwise delete
    # a pinned session's artefacts on the next tick. Skipping a sweep is
    # recoverable; an rmtree is not.
    try:
        from db.sessions import pinned_job_legacy_ids
        protected = pinned_job_legacy_ids()
    except Exception as e:
        logger.error(
            "Cleanup aborted: could not resolve pinned jobs (%s). Skipping "
            "this sweep rather than risk deleting protected artefacts.", e,
        )
        return {'status': 'error', 'error': f'pinned-job lookup failed: {e}',
                'deleted_count': 0, 'deleted_jobs': []}

    try:
        # Perform cleanup (not a dry run)
        deleted_jobs = ResourceManager.cleanup_old_jobs(
            dry_run=False, protected_job_ids=protected,
        )

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
                select(SessionRow).where(
                    SessionRow.expired_at.is_(None),
                    SessionRow.pinned.is_(False),
                )
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
                Session.pinned.is_(False),
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
