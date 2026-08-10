from celery import Celery
from celery.schedules import crontab
from config import Config

celery_app = Celery(
    'pockethunter_tasks',
    broker=Config.CELERY_BROKER_URL,
    backend=Config.CELERY_RESULT_BACKEND,
    # C5: cleanup_job listed so its @celery_app.task definitions actually
    # register with workers. Pre-C5 only `tasks` was here, so the daily
    # cleanup-old-jobs and check-disk-usage beat schedules failed with
    # NotRegistered when they fired — caught during C3 verification.
    include=['tasks', 'cleanup_job']
)

celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    broker_connection_retry_on_startup=True,  # Important for robust startup
    # Survive worker death mid-task. With ack-late the broker only
    # considers a task done after the worker successfully completes it,
    # so a SIGKILLed worker (orchestrator force-recycle, OOM, host
    # reboot) causes the task to be re-delivered to another worker
    # instead of being silently lost. Requires task idempotency:
    # extract/detect/cluster overwrite per-job-id dirs (safe); docking
    # is made idempotent by pair-level resume (tasks.run_docking_task
    # reads existing docking_results.csv and skips done pairs).
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Distinguish 'queued in broker' (PENDING) from 'worker picked it
    # up and is executing' (STARTED). Required by the docking panel's
    # re-attach logic: PENDING with a stale Job row means the task is
    # genuinely lost, not just slow to start.
    task_track_started=True,
)

# Route docking tasks to the dedicated queue so they can't starve
# find_pockets / cluster jobs sharing the default queue.
celery_app.conf.task_routes = {
    'tasks.run_docking_task': {'queue': 'docking'},
    'tasks.run_find_pockets_task': {'queue': 'default'},
    'tasks.run_cluster_pockets_task': {'queue': 'default'},
}
celery_app.conf.task_default_queue = 'default'
celery_app.conf.worker_prefetch_multiplier = 1

# Configure Celery Beat schedule for periodic tasks
celery_app.conf.beat_schedule = {
    'cleanup-old-jobs': {
        'task': 'cleanup_job.cleanup_old_jobs_task',
        'schedule': crontab(hour=2, minute=0),  # Run daily at 2 AM
        'options': {'expires': 3600}  # Task expires after 1 hour if not executed
    },
    'check-disk-usage': {
        'task': 'cleanup_job.check_disk_usage_task',
        'schedule': crontab(minute='*/30'),  # Run every 30 minutes
        'options': {'expires': 1800}  # Task expires after 30 minutes
    },
    # C5: per-session disk quota pruning. Runs hourly because abuse
    # scenarios (one session uploading repeatedly) need a faster cadence
    # than the daily "old jobs" sweep.
    'enforce-session-disk-quotas': {
        'task': 'cleanup_job.enforce_session_disk_quotas_task',
        'schedule': crontab(minute=15),  # 15 past every hour
        'options': {'expires': 1800},
    },
    # Delete abandoned sessions (no jobs, older than SESSION_GRACE_MINUTES)
    # so they don't pollute the DB AND don't count toward the per-IP
    # daily limit. Fine-grained cadence — the user-visible quota should
    # reflect reality within ~5 min of the grace window expiring.
    'cleanup-abandoned-sessions': {
        'task': 'cleanup_job.cleanup_abandoned_sessions_task',
        'schedule': crontab(minute='*/5'),
        'options': {'expires': 240},
    },
    # Reap Job rows an actually-dead worker never got to finish (see the
    # module comment on cleanup_job.reap_stale_jobs_task for the defect and
    # the STALE_JOB_CUTOFF_SECONDS derivation). This is slow-moving by
    # nature — the shortest per-kind cutoff is ~1.25h (cluster) and the
    # longest ~2.33h (docking), so nothing legitimate could be reaped even
    # at a much coarser cadence than the other beat tasks here. Hourly keeps
    # a phantom row's lifetime bounded (worst case: one cutoff period plus
    # one hour) without adding meaningful DB load.
    'reap-stale-jobs': {
        'task': 'cleanup_job.reap_stale_jobs_task',
        'schedule': crontab(minute=45),  # 45 past every hour
        'options': {'expires': 1800},
    },
}

if __name__ == '__main__':
    celery_app.start() 