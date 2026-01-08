from celery import Celery
from celery.schedules import crontab
from config import Config

celery_app = Celery(
    'pockethunter_tasks',
    broker=Config.CELERY_BROKER_URL,
    backend=Config.CELERY_RESULT_BACKEND,
    include=['tasks']  # List of modules to import when a worker starts
)

celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    broker_connection_retry_on_startup=True  # Important for robust startup
)

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
}

if __name__ == '__main__':
    celery_app.start() 