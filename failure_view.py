"""Unified failure rendering for the PocketHunter Suite.

Two layers:

1. **Pure classifier** — ``classify_error(task_info) -> ClassifiedError``.
   Maps an exception type + message to one of six categories with a
   user-facing headline and a suggested next step. Pure (no Streamlit,
   no I/O). Unit-tested in ``tests/test_failure_view.py``.

2. **Streamlit renderer** — ``render_task_failure(task_info, status_json,
   job_id, on_retry=None)``. Imports Streamlit at call-time so the
   classifier can be imported by Celery workers without booting Streamlit.
"""
from __future__ import annotations

import enum
import os
from dataclasses import dataclass
from typing import Callable, Optional


class ErrorCategory(str, enum.Enum):
    VALIDATION = "validation"
    SUBPROCESS_CRASH = "subprocess_crash"
    TIMEOUT = "timeout"
    NO_OUTPUT = "no_output"
    DEPENDENCY_MISSING = "dependency_missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedError:
    category: ErrorCategory
    headline: str
    suggestion: str


# Exception type → category mapping (checked first; cheap and authoritative
# when the raising code is ours).
_EXC_TYPE_MAP: dict[str, ErrorCategory] = {
    "ValueError": ErrorCategory.VALIDATION,
    "SoftTimeLimitExceeded": ErrorCategory.TIMEOUT,
    "TimeoutExpired": ErrorCategory.TIMEOUT,
    "DetectionProducedNoOutput": ErrorCategory.NO_OUTPUT,
    "ClusteringFoundNoClusters": ErrorCategory.NO_OUTPUT,
    "DockingProducedNoResults": ErrorCategory.NO_OUTPUT,
    "CalledProcessError": ErrorCategory.SUBPROCESS_CRASH,
}


def classify_error(task_info: Optional[dict]) -> ClassifiedError:
    """Map a Celery FAILURE meta dict (or our status-JSON ``error`` dict) to a category."""
    if not task_info or not isinstance(task_info, dict):
        return _unknown()

    exc_type = str(task_info.get("exc_type", ""))
    exc_message = str(task_info.get("exc_message", ""))
    msg_lower = exc_message.lower()

    # FileNotFoundError can be either "missing dependency" (smina/p2rank binary)
    # or "missing input" (uploaded file gone). Disambiguate on the message.
    if exc_type == "FileNotFoundError":
        if any(t in msg_lower for t in ("smina", "prank", "p2rank", "/usr/local/bin", "ligand", "pdbqt")):
            return _dependency_missing(exc_message)
        return _validation(exc_message)

    category = _EXC_TYPE_MAP.get(exc_type)

    # Generic Exception with subprocess-style "Stderr: ..." tail
    if category is None and "stderr:" in msg_lower:
        category = ErrorCategory.SUBPROCESS_CRASH

    if category is None:
        return _unknown(exc_message)

    if category == ErrorCategory.VALIDATION:
        return _validation(exc_message)
    if category == ErrorCategory.TIMEOUT:
        return _timeout(exc_message)
    if category == ErrorCategory.NO_OUTPUT:
        return _no_output(exc_type, exc_message)
    if category == ErrorCategory.SUBPROCESS_CRASH:
        return _subprocess_crash(exc_message)
    if category == ErrorCategory.DEPENDENCY_MISSING:
        return _dependency_missing(exc_message)
    return _unknown(exc_message)


# ── Category builders — each owns its headline + suggestion ──────────────


def _validation(msg: str) -> ClassifiedError:
    return ClassifiedError(
        category=ErrorCategory.VALIDATION,
        headline="Input validation failed",
        suggestion=(
            "The task rejected its inputs before doing any work. Check the "
            "uploaded files and parameters above, fix the issue, and resubmit."
        ),
    )


def _timeout(msg: str) -> ClassifiedError:
    return ClassifiedError(
        category=ErrorCategory.TIMEOUT,
        headline="Task timed out",
        suggestion=(
            "The job ran longer than its configured timeout. Try a smaller "
            "trajectory, increase the stride parameter (fewer frames), or run "
            "the steps individually so each one has its own time budget."
        ),
    )


def _no_output(exc_type: str, msg: str) -> ClassifiedError:
    if exc_type == "ClusteringFoundNoClusters":
        return ClassifiedError(
            category=ErrorCategory.NO_OUTPUT,
            headline="Clustering produced no clusters",
            suggestion=(
                "DBSCAN found no dense pocket regions at the current threshold. "
                "Lower min_prob (try 0.3 or 0.2), reduce the trajectory stride "
                "to get more frames, or switch to the Hierarchical method."
            ),
        )
    if exc_type == "DetectionProducedNoOutput":
        return ClassifiedError(
            category=ErrorCategory.NO_OUTPUT,
            headline="Pocket detection produced no output",
            suggestion=(
                "p2rank ran but no pockets.csv was created — it likely crashed "
                "silently (Java OOM or malformed input PDB). Open the error log "
                "below to see the raw p2rank output."
            ),
        )
    if exc_type == "DockingProducedNoResults":
        return ClassifiedError(
            category=ErrorCategory.NO_OUTPUT,
            headline="Docking produced no poses",
            suggestion=(
                "Every receptor/ligand pair failed or yielded no poses. Check "
                "the ligand files (must be PDBQT) and the box size — if the box "
                "is too small, smina can't place anything."
            ),
        )
    return ClassifiedError(
        category=ErrorCategory.NO_OUTPUT,
        headline="Task produced no output",
        suggestion="The job ran but produced no usable result. Check the error log.",
    )


def _subprocess_crash(msg: str) -> ClassifiedError:
    msg_lower = msg.lower()
    if "outofmemory" in msg_lower or "java heap" in msg_lower or "exit 137" in msg_lower:
        return ClassifiedError(
            category=ErrorCategory.SUBPROCESS_CRASH,
            headline="Subprocess crashed (out of memory)",
            suggestion=(
                "A subprocess hit an OOM and was killed. Run with a smaller "
                "trajectory or fewer frames (increase the stride parameter), "
                "or give the workers more memory."
            ),
        )
    if "exit 139" in msg_lower or "segfault" in msg_lower or "signal 11" in msg_lower:
        return ClassifiedError(
            category=ErrorCategory.SUBPROCESS_CRASH,
            headline="Subprocess segfaulted",
            suggestion=(
                "A subprocess crashed with a segmentation fault. This usually "
                "means a malformed input (corrupt PDB or PDBQT) or a binary "
                "compatibility issue. Inspect the error log."
            ),
        )
    return ClassifiedError(
        category=ErrorCategory.SUBPROCESS_CRASH,
        headline="Subprocess failed",
        suggestion=(
            "An external tool exited with an error. The error log has the "
            "full stderr — usually it points at a bad input or a missing "
            "dependency."
        ),
    )


def _dependency_missing(msg: str) -> ClassifiedError:
    return ClassifiedError(
        category=ErrorCategory.DEPENDENCY_MISSING,
        headline="Required tool or file not found",
        suggestion=(
            "A required binary (smina, p2rank) or input file is missing. "
            "Check the deployment: smina should be installed at SMINA_PATH "
            "and PocketHunter's first_setup.sh should have downloaded p2rank."
        ),
    )


def _unknown(msg: str = "") -> ClassifiedError:
    return ClassifiedError(
        category=ErrorCategory.UNKNOWN,
        headline="Task failed",
        suggestion=(
            "The error didn't match any known pattern. Inspect the full message "
            "below — and consider opening a bug report with the error log attached."
        ),
    )


# ── Status JSON loader ───────────────────────────────────────────────────


def load_status_json(job_id: Optional[str]) -> Optional[dict]:
    """Read ``<results>/<job_id>_status.json`` or return None.

    Used as a fallback when the Celery result backend has expired but the
    on-disk status file still has the structured ``error`` dict (written by
    ``tasks._fail_job``).
    """
    if not job_id:
        return None
    import json
    try:
        from config import Config
        path = os.path.join(str(Config.RESULTS_DIR), f"{job_id}_status.json")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ── Streamlit renderer ───────────────────────────────────────────────────


def render_task_failure(
    task_info: Optional[dict],
    status_json: Optional[dict] = None,
    job_id: Optional[str] = None,
    *,
    on_retry: Optional[Callable[[], None]] = None,
) -> None:
    """Render a structured failure panel.

    ``task_info`` is the live Celery ``task.info`` meta dict (richest source).
    ``status_json`` is the on-disk ``<job>_status.json`` dict — used as a
    fallback when the Celery result has expired, and as the source for the
    ``error.log`` path.
    """
    import streamlit as st  # local import — pure classifier stays Streamlit-free

    # Merge sources: prefer task_info but fall back to status_json['error'].
    source = task_info or {}
    if status_json and isinstance(status_json.get("error"), dict):
        for k, v in status_json["error"].items():
            source.setdefault(k, v)

    result = classify_error(source)

    st.error(f"**{result.headline}**")
    st.info(result.suggestion)

    exc_message = source.get("exc_message") or source.get("status") or ""
    if exc_message:
        with st.expander("Show full error details"):
            st.code(str(exc_message), language="text")

    # Per-job error.log — read from status_json.error.log_path or derive.
    log_path = (status_json or {}).get("error", {}).get("log_path") if isinstance(status_json, dict) else None
    if not log_path and job_id:
        # Default location.
        from config import Config
        candidate = os.path.join(str(Config.RESULTS_DIR), job_id, "error.log")
        if os.path.exists(candidate):
            log_path = candidate

    if log_path and os.path.exists(log_path):
        try:
            with open(log_path, "rb") as f:
                st.download_button(
                    "Download error log",
                    data=f.read(),
                    file_name=f"error_{job_id or 'job'}.log",
                    mime="text/plain",
                )
        except OSError:
            pass

    if on_retry is not None:
        if st.button("Retry", key=f"retry_{job_id or 'unknown'}", type="primary"):
            on_retry()
