"""
Rate limiting module for PocketHunter-Suite.

Provides configurable rate limiting for uploads and task submissions.
Can be disabled for local development via RATE_LIMIT_ENABLED=false in .env.
"""

import time
from collections import deque
from typing import Optional
import streamlit as st
from config import Config
from logging_config import setup_logging

logger = setup_logging(__name__)


class RateLimitExceeded(Exception):
    """Raised when rate limit is exceeded."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimiter:
    """
    Sliding window rate limiter using Streamlit session state.

    This implementation uses session state to persist rate limit data
    across Streamlit reruns within the same session.
    """

    def __init__(self, name: str, max_requests: int, window_seconds: int):
        """
        Initialize rate limiter.

        Args:
            name: Unique name for this rate limiter (used as session state key)
            max_requests: Maximum number of requests allowed in the window
            window_seconds: Time window in seconds
        """
        self.name = name
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._state_key = f"_rate_limiter_{name}"

    def _get_timestamps(self) -> deque:
        """Get or initialize the timestamps deque from session state."""
        if self._state_key not in st.session_state:
            st.session_state[self._state_key] = deque()
        return st.session_state[self._state_key]

    def _cleanup_old_timestamps(self, timestamps: deque) -> None:
        """Remove timestamps outside the current window."""
        current_time = time.time()
        cutoff = current_time - self.window_seconds

        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()

    def check_rate_limit(self) -> bool:
        """
        Check if the rate limit allows a new request.

        Returns:
            True if request is allowed, False otherwise
        """
        if not Config.RATE_LIMIT_ENABLED:
            return True

        timestamps = self._get_timestamps()
        self._cleanup_old_timestamps(timestamps)

        return len(timestamps) < self.max_requests

    def get_retry_after(self) -> float:
        """
        Get seconds until rate limit resets.

        Returns:
            Seconds until the oldest request expires from the window
        """
        timestamps = self._get_timestamps()
        self._cleanup_old_timestamps(timestamps)

        if not timestamps:
            return 0.0

        oldest = timestamps[0]
        current_time = time.time()
        retry_after = (oldest + self.window_seconds) - current_time

        return max(0.0, retry_after)

    def record_request(self) -> None:
        """Record a new request timestamp."""
        if not Config.RATE_LIMIT_ENABLED:
            return

        timestamps = self._get_timestamps()
        self._cleanup_old_timestamps(timestamps)
        timestamps.append(time.time())

    def acquire(self) -> None:
        """
        Attempt to acquire a rate limit slot.

        Raises:
            RateLimitExceeded: If rate limit is exceeded
        """
        if not Config.RATE_LIMIT_ENABLED:
            return

        if not self.check_rate_limit():
            retry_after = self.get_retry_after()
            raise RateLimitExceeded(
                f"Rate limit exceeded for {self.name}. "
                f"Maximum {self.max_requests} requests per {self.window_seconds} seconds. "
                f"Try again in {retry_after:.1f} seconds.",
                retry_after=retry_after
            )

        self.record_request()

    def get_remaining(self) -> int:
        """
        Get number of remaining requests in current window.

        Returns:
            Number of requests remaining
        """
        if not Config.RATE_LIMIT_ENABLED:
            return self.max_requests

        timestamps = self._get_timestamps()
        self._cleanup_old_timestamps(timestamps)

        return max(0, self.max_requests - len(timestamps))


# Global rate limiter instances
upload_limiter = RateLimiter(
    name="uploads",
    max_requests=Config.RATE_LIMIT_MAX_UPLOADS,
    window_seconds=Config.RATE_LIMIT_WINDOW_SECONDS
)

task_limiter = RateLimiter(
    name="tasks",
    max_requests=Config.RATE_LIMIT_MAX_TASKS,
    window_seconds=Config.RATE_LIMIT_TASK_WINDOW_SECONDS
)


def check_upload_rate_limit() -> None:
    """
    Check and acquire upload rate limit.

    Raises:
        RateLimitExceeded: If upload rate limit is exceeded
    """
    upload_limiter.acquire()


def check_task_rate_limit() -> None:
    """
    Check and acquire task submission rate limit.

    Raises:
        RateLimitExceeded: If task rate limit is exceeded
    """
    task_limiter.acquire()


def get_rate_limit_status() -> dict:
    """
    Get current rate limit status for display.

    Returns:
        Dictionary with rate limit information
    """
    return {
        "enabled": Config.RATE_LIMIT_ENABLED,
        "uploads": {
            "remaining": upload_limiter.get_remaining(),
            "max": Config.RATE_LIMIT_MAX_UPLOADS,
            "window_seconds": Config.RATE_LIMIT_WINDOW_SECONDS,
            "retry_after": upload_limiter.get_retry_after() if not upload_limiter.check_rate_limit() else 0
        },
        "tasks": {
            "remaining": task_limiter.get_remaining(),
            "max": Config.RATE_LIMIT_MAX_TASKS,
            "window_seconds": Config.RATE_LIMIT_TASK_WINDOW_SECONDS,
            "retry_after": task_limiter.get_retry_after() if not task_limiter.check_rate_limit() else 0
        }
    }
