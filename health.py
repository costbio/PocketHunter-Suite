"""
Health check utilities for monitoring PocketHunter-Suite services.

This module provides health check functions for:
- Redis connectivity
- Celery worker availability
- Disk space usage

Used by monitoring systems and the web UI to verify system health.
"""

import redis
from celery_app import celery_app
from config import Config
from resource_manager import ResourceManager
from logging_config import setup_logging
from typing import Dict, Any

logger = setup_logging(__name__)


def check_redis() -> Dict[str, Any]:
    """
    Check Redis connectivity and availability.

    Returns:
        Dictionary with status and message:
        - status: 'healthy' or 'unhealthy'
        - message: Description of the status
        - response_time_ms: Response time in milliseconds (if healthy)

    Example:
        >>> result = check_redis()
        >>> print(result)
        {'status': 'healthy', 'message': 'Redis accessible', 'response_time_ms': 2.5}
    """
    import time
    try:
        r = redis.from_url(Config.CELERY_BROKER_URL, socket_connect_timeout=2)
        start = time.time()
        r.ping()
        response_time = (time.time() - start) * 1000  # Convert to ms

        return {
            'status': 'healthy',
            'message': 'Redis accessible',
            'response_time_ms': round(response_time, 2)
        }
    except redis.ConnectionError as e:
        logger.error(f"Redis connection error: {e}")
        return {
            'status': 'unhealthy',
            'message': f'Redis connection failed: {e}'
        }
    except Exception as e:
        logger.error(f"Redis health check error: {e}")
        return {
            'status': 'unhealthy',
            'message': f'Redis error: {e}'
        }


def check_celery() -> Dict[str, Any]:
    """
    Check Celery worker availability.

    Returns:
        Dictionary with status and worker information:
        - status: 'healthy' or 'unhealthy'
        - message: Description of the status
        - workers: Number of active workers (if healthy)
        - worker_names: List of active worker names (if healthy)

    Example:
        >>> result = check_celery()
        >>> print(result)
        {'status': 'healthy', 'workers': 2, 'worker_names': ['celery@worker1', 'celery@worker2']}
    """
    try:
        inspector = celery_app.control.inspect(timeout=2.0)
        active_workers = inspector.active()

        if active_workers:
            worker_count = len(active_workers)
            worker_names = list(active_workers.keys())

            return {
                'status': 'healthy',
                'message': f'{worker_count} worker(s) active',
                'workers': worker_count,
                'worker_names': worker_names
            }
        else:
            logger.warning("No active Celery workers found")
            return {
                'status': 'unhealthy',
                'message': 'No active workers',
                'workers': 0
            }

    except Exception as e:
        logger.error(f"Celery health check error: {e}")
        return {
            'status': 'unhealthy',
            'message': f'Celery inspection failed: {e}',
            'workers': 0
        }


def check_disk_space() -> Dict[str, Any]:
    """
    Check disk space usage.

    Returns:
        Dictionary with disk usage information:
        - status: 'healthy', 'warning', or 'critical'
        - usage_pct: Disk usage percentage
        - used_gb: Used space in GB
        - limit_gb: Configured limit in GB
        - available_gb: Available space in GB

    Example:
        >>> result = check_disk_space()
        >>> print(result)
        {'status': 'healthy', 'usage_pct': 45.2, 'used_gb': 45.2, 'limit_gb': 100, 'available_gb': 54.8}
    """
    try:
        used, limit, usage_pct = ResourceManager.check_disk_usage()

        # Determine status based on usage
        if usage_pct > 90:
            status = 'critical'
        elif usage_pct > 75:
            status = 'warning'
        else:
            status = 'healthy'

        return {
            'status': status,
            'usage_pct': round(usage_pct, 2),
            'used_gb': round(used / (1024 ** 3), 2),
            'limit_gb': Config.MAX_DISK_USAGE_GB,
            'available_gb': round((limit - used) / (1024 ** 3), 2)
        }

    except Exception as e:
        logger.error(f"Disk space check error: {e}")
        return {
            'status': 'unknown',
            'message': f'Error checking disk space: {e}'
        }


def health_check() -> Dict[str, Any]:
    """
    Comprehensive health check of all system components.

    Checks Redis, Celery workers, and disk space.
    Overall status is 'healthy' only if all components are healthy
    (disk can be 'warning' and still be considered healthy).

    Returns:
        Dictionary with overall status and component details:
        - status: 'healthy' or 'unhealthy'
        - timestamp: ISO format timestamp
        - components: Dictionary of component health checks

    Example:
        >>> result = health_check()
        >>> print(result['status'])
        'healthy'
        >>> print(result['components']['redis']['status'])
        'healthy'
    """
    from datetime import datetime

    redis_health = check_redis()
    celery_health = check_celery()
    disk_health = check_disk_space()

    # Overall healthy if Redis and Celery are healthy,
    # and disk is not critical
    redis_ok = redis_health['status'] == 'healthy'
    celery_ok = celery_health['status'] == 'healthy'
    disk_ok = disk_health['status'] in ['healthy', 'warning']

    overall_healthy = redis_ok and celery_ok and disk_ok

    result = {
        'status': 'healthy' if overall_healthy else 'unhealthy',
        'timestamp': datetime.now().isoformat(),
        'components': {
            'redis': redis_health,
            'celery': celery_health,
            'disk': disk_health
        }
    }

    # Log the health check result
    if overall_healthy:
        logger.debug(f"Health check passed: {result}")
    else:
        logger.warning(f"Health check failed: {result}")

    return result


def get_system_info() -> Dict[str, Any]:
    """
    Get detailed system information for monitoring dashboard.

    Returns comprehensive information about:
    - Configuration
    - Resource usage
    - Oldest jobs
    - System health

    Returns:
        Dictionary with system information

    Example:
        >>> info = get_system_info()
        >>> print(info['config']['max_upload_size_mb'])
        500
    """
    try:
        usage_report = ResourceManager.get_usage_report()
        oldest_jobs = ResourceManager.get_oldest_jobs(10)
        health = health_check()

        return {
            'health': health,
            'config': {
                'max_upload_size_mb': Config.MAX_UPLOAD_SIZE / (1024 ** 2),
                'max_zip_size_gb': Config.MAX_ZIP_SIZE / (1024 ** 3),
                'cleanup_after_days': Config.CLEANUP_AFTER_DAYS,
                'max_disk_usage_gb': Config.MAX_DISK_USAGE_GB
            },
            'usage': usage_report,
            'oldest_jobs': oldest_jobs
        }

    except Exception as e:
        logger.error(f"Error getting system info: {e}", exc_info=True)
        return {
            'error': str(e),
            'health': {'status': 'unknown'}
        }


if __name__ == '__main__':
    # Test health checks
    print("=" * 60)
    print("HEALTH CHECK TEST")
    print("=" * 60)

    print("\n1. Redis Health:")
    redis_result = check_redis()
    print(f"   Status: {redis_result['status']}")
    print(f"   Message: {redis_result['message']}")

    print("\n2. Celery Health:")
    celery_result = check_celery()
    print(f"   Status: {celery_result['status']}")
    print(f"   Message: {celery_result['message']}")

    print("\n3. Disk Space:")
    disk_result = check_disk_space()
    print(f"   Status: {disk_result['status']}")
    print(f"   Usage: {disk_result.get('usage_pct', 'N/A')}%")

    print("\n4. Overall Health:")
    overall = health_check()
    print(f"   Status: {overall['status']}")
    print(f"   Timestamp: {overall['timestamp']}")

    print("\n✅ Health check tests complete!")
