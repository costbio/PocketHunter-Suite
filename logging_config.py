"""
Centralized logging configuration for PocketHunter-Suite.

This module provides structured logging with:
- Console output for immediate feedback
- File output with rotation for persistent logs
- Configurable log levels
- Consistent formatting across all modules
"""

import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
from config import Config


def setup_logging(name: str = 'pockethunter') -> logging.Logger:
    """
    Configure and return a logger instance.

    Creates a logger with both console and file handlers.
    Console handler shows INFO+ messages, file handler shows DEBUG+ messages.
    File handler uses rotation to prevent unbounded log growth.

    Args:
        name: Logger name (typically __name__ from calling module)

    Returns:
        Configured logger instance

    Example:
        >>> logger = setup_logging(__name__)
        >>> logger.info("Application started")
        2026-01-09 12:00:00 - mymodule - INFO - Application started
    """
    logger = logging.getLogger(name)

    # Only configure if not already configured (prevents duplicate handlers)
    if logger.handlers:
        return logger

    # Set base log level from config
    log_level = getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(log_level)

    # ========================================
    # Console Handler (stdout)
    # ========================================
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)  # Only show INFO+ on console

    console_formatter = logging.Formatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)

    # ========================================
    # File Handler (with rotation)
    # ========================================
    log_file = Path(Config.LOG_FILE)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=5,               # Keep 5 backup files (50 MB total)
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)  # Capture everything in file

    file_formatter = logging.Formatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)

    # ========================================
    # Add Handlers to Logger
    # ========================================
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger


def log_exception(logger: logging.Logger, exc: Exception, context: str = "") -> None:
    """
    Log an exception with full traceback and context.

    Args:
        logger: Logger instance
        exc: Exception to log
        context: Additional context about where/why the exception occurred

    Example:
        >>> logger = setup_logging(__name__)
        >>> try:
        ...     risky_operation()
        ... except Exception as e:
        ...     log_exception(logger, e, "Failed during file upload")
    """
    if context:
        logger.error(f"{context}: {exc}", exc_info=True)
    else:
        logger.error(f"Exception occurred: {exc}", exc_info=True)


def log_celery_task_start(logger: logging.Logger, task_name: str, task_id: str, **kwargs) -> None:
    """
    Log the start of a Celery task with parameters.

    Args:
        logger: Logger instance
        task_name: Name of the task
        task_id: Celery task ID
        **kwargs: Task parameters to log

    Example:
        >>> logger = setup_logging(__name__)
        >>> log_celery_task_start(
        ...     logger,
        ...     "run_extract_to_pdb",
        ...     "abc-123",
        ...     xtc_file="trajectory.xtc",
        ...     stride=10
        ... )
    """
    params_str = ", ".join(f"{k}={v}" for k, v in kwargs.items())
    logger.info(f"Task started: {task_name} [ID: {task_id}] with params: {params_str}")


def log_celery_task_complete(logger: logging.Logger, task_name: str, task_id: str, duration: float = None) -> None:
    """
    Log the completion of a Celery task.

    Args:
        logger: Logger instance
        task_name: Name of the task
        task_id: Celery task ID
        duration: Optional task duration in seconds

    Example:
        >>> logger = setup_logging(__name__)
        >>> log_celery_task_complete(logger, "run_extract_to_pdb", "abc-123", 45.2)
    """
    if duration:
        logger.info(f"Task completed: {task_name} [ID: {task_id}] in {duration:.2f}s")
    else:
        logger.info(f"Task completed: {task_name} [ID: {task_id}]")


def log_celery_task_failed(logger: logging.Logger, task_name: str, task_id: str, error: Exception) -> None:
    """
    Log a failed Celery task.

    Args:
        logger: Logger instance
        task_name: Name of the task
        task_id: Celery task ID
        error: Exception that caused the failure

    Example:
        >>> logger = setup_logging(__name__)
        >>> log_celery_task_failed(logger, "run_extract_to_pdb", "abc-123", ValueError("Invalid input"))
    """
    logger.error(f"Task failed: {task_name} [ID: {task_id}] - {error}", exc_info=True)


# Create a default logger for the application
default_logger = setup_logging('pockethunter')


if __name__ == '__main__':
    # Test logging configuration
    logger = setup_logging('test')

    logger.debug("This is a debug message")
    logger.info("This is an info message")
    logger.warning("This is a warning message")
    logger.error("This is an error message")
    logger.critical("This is a critical message")

    try:
        raise ValueError("Test exception")
    except Exception as e:
        log_exception(logger, e, "Testing exception logging")

    print(f"\n✅ Logging configured successfully!")
    print(f"Log file: {Config.LOG_FILE}")
