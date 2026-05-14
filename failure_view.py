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
        # Two distinct sub-causes share the type. The message (raised by
        # tasks.py) distinguishes them; we surface a guidance line that
        # covers both. The live-log panel at the bottom of the analysis
        # page lets the user see what p2rank actually emitted.
        return ClassifiedError(
            category=ErrorCategory.NO_OUTPUT,
            headline="Pocket detection produced no pockets",
            suggestion=(
                "Either p2rank crashed silently, or it ran cleanly but found "
                "no pockets above its default probability threshold (common on "
                "small / flat-surface proteins like T4 lysozyme). Check the "
                "**Live log** panel below the columns + the persisted "
                "`.live/detecting_pockets.stderr.log` under the job's results "
                "directory to distinguish — a clean run emits a normal p2rank "
                "report; a crash emits a stack trace."
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


def load_status_for_session(session_id) -> list[dict]:
    """Return one status-dict per ``Job`` row belonging to a session.

    v2 Phase A3: read side for the DB mirror. Each returned dict has the
    same shape the disk ``<job>_status.json`` files use, so callers can
    feed them into ``classify_error`` / ``render_task_failure`` /
    ``render_pair_failures_callout`` unchanged.

    Used by the eventual session-scoped Task Monitor in Phase B. Returns
    ``[]`` on any DB error so caller code can stay terse.
    """
    if session_id is None:
        return []
    try:
        from db.jobs import find_by_session

        rows = find_by_session(session_id)
        return [
            {
                "status": r.status,
                "step": r.step,
                "task_id": r.celery_task_id,
                "kind": r.kind,
                "result_info": r.result_info,
                "error": r.error,
                "pair_failures": r.pair_failures,
                "pair_failures_log": r.pair_failures_log,
                "legacy_id": r.legacy_id,
                "last_updated": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
    except Exception:
        return []


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


def render_pair_failures_callout(task_result: Optional[dict], job_id: Optional[str] = None) -> None:
    """Render a partial-success callout when some docking pairs failed.

    Reads ``pairs_failed``, ``pairs_total``, ``pair_failures``, and
    ``pair_failures_log`` from ``task_result`` (the dict returned by
    ``run_docking_task`` / ``run_pockethunter_pipeline`` on SUCCESS).
    No-op if ``pairs_failed`` is missing or zero — fully successful tasks
    render nothing.
    """
    import streamlit as st

    from docking_pair_failures import summarize_pair_failures

    if not task_result or not isinstance(task_result, dict):
        return
    pairs_failed = int(task_result.get("pairs_failed") or 0)
    if pairs_failed <= 0:
        return

    pairs_total = int(task_result.get("pairs_total") or 0)
    pair_failures = task_result.get("pair_failures") or []
    headline = (
        f"⚠️ {pairs_failed} of {pairs_total} docking pairs failed — "
        "the results below cover only the successful pairs."
    )
    st.warning(headline)
    st.caption(summarize_pair_failures(pair_failures))

    log_path = task_result.get("pair_failures_log")
    if log_path and os.path.exists(str(log_path)):
        try:
            with open(str(log_path), "rb") as f:
                st.download_button(
                    "Download docking failure log",
                    data=f.read(),
                    file_name=f"docking_pair_failures_{job_id or 'job'}.log",
                    mime="text/plain",
                    key=f"pair_fail_log_dl_{job_id or 'job'}",
                )
        except OSError:
            pass

    if pair_failures:
        with st.expander(f"Pairs that failed ({pairs_failed})"):
            for rec in pair_failures:
                line = (
                    f"• receptor=`{rec.get('receptor', '?')}` · "
                    f"ligand=`{rec.get('ligand', '?')}` — "
                    f"**{rec.get('exc_type', 'Unknown')}**"
                )
                msg = (rec.get("exc_message") or "").splitlines()[0] if rec.get("exc_message") else ""
                if msg:
                    line += f" — {msg[:200]}"
                st.markdown(line)


def render_ligand_conversion_callout(
    task_result: Optional[dict], job_id: Optional[str] = None
) -> None:
    """Render a callout when obabel converted fewer ligands than uploaded.

    Reads ``ligand_conversion_failures`` from ``task_result`` — the list
    of ``{source_file, expected, converted, error}`` records that
    ``run_docking_task`` carries on its SUCCESS result. No-op when that
    list is absent or empty (a clean conversion renders nothing).

    Sibling of :func:`render_pair_failures_callout`: a docking *pair*
    failure and a ligand *conversion* failure are distinct modes, so
    they get distinct callouts.
    """
    import streamlit as st

    if not task_result or not isinstance(task_result, dict):
        return
    failures = task_result.get("ligand_conversion_failures") or []
    if not failures:
        return

    total_expected = sum(int(f.get("expected") or 0) for f in failures)
    total_converted = sum(int(f.get("converted") or 0) for f in failures)
    n_lost = total_expected - total_converted
    st.warning(
        f"⚠️ {n_lost} of {total_expected} ligand(s) failed to convert "
        "(malformed SDF/PDB records) — the results below cover only the "
        f"{total_converted} that converted."
    )

    with st.expander(f"Files with conversion shortfalls ({len(failures)})"):
        for f in failures:
            st.markdown(
                f"• `{f.get('source_file', '?')}` — converted "
                f"**{f.get('converted', 0)} of {f.get('expected', 0)}** molecules"
            )
            err = (f.get("error") or "").splitlines()[0] if f.get("error") else ""
            if err:
                st.caption(err[:300])
