"""Live-log helpers for the v2 analysis app (B8).

The persistent Mol* viewer + the jobs panel cover *what* has happened
on a session. This module covers *what's happening right now*: tailing
the active stage's stdout / stderr while a Celery task runs, so the
user sees p2rank / smina / mdtraj chatter the moment it's emitted
rather than at task completion.

Three small pure helpers, called from a fragment in ``analysis_app``:

* :func:`tail_log` — read the last N lines from a (possibly growing)
  file. Tolerates missing files.
* :func:`find_active_stage_log` — locate the currently-running stage's
  stdout log under ``results/<legacy_id>/.live/`` for the session's
  most-recent in-flight job.
* :func:`any_task_running` — does the session have at least one job
  whose ``status`` is ``running`` / ``submitted`` / ``PENDING`` /
  ``PROGRESS`` / ``STARTED``?

The ``_run_stage`` helper in :mod:`tasks` writes the actual log files
under ``results/<legacy_id>/.live/<stage>.stdout.log`` (and ``.stderr.log``).
Those files are kept around after stage completion for post-hoc
diagnosis — cleanup_job prunes them along with the rest of the job
directory.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Optional


# Status values that mean "this job is still doing work".
_RUNNING_STATUSES = frozenset(
    {"running", "submitted", "PENDING", "PROGRESS", "STARTED", "RECEIVED", "RETRY"}
)


def sanitize_stage_name(stage_name: str) -> str:
    """Turn a human stage label into a filesystem-safe filename stem.

    ``"Detecting pockets"`` → ``"detecting_pockets"``. Matches the
    sanitization ``_run_stage`` uses, so the writer + reader agree on
    the path.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", stage_name).strip("_").lower()
    return cleaned or "stage"


def tail_log(path: Path, max_lines: int = 50) -> str:
    """Return the last ``max_lines`` lines from ``path`` as a single string.

    Missing or unreadable files return ``""`` — the caller renders an
    empty-state caption instead of crashing.

    Implementation reads from the end with a coarse byte budget; for
    typical p2rank / smina output (~few KB per stage) this is fine.
    Pure: no Streamlit, no I/O side-effects beyond the read.
    """
    try:
        p = Path(path)
        if not p.exists():
            return ""
        size = p.stat().st_size
        if size == 0:
            return ""
        # ~256 bytes per line is a generous upper bound for p2rank output.
        budget = max_lines * 256 + 1024
        truncated = size > budget
        with open(p, "rb") as fh:
            if truncated:
                fh.seek(-budget, os.SEEK_END)
            data = fh.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        # When we read from the middle of the file, the first line is
        # likely a partial record — drop it so we don't show garbage.
        if truncated and lines:
            lines = lines[1:]
        return "\n".join(lines[-max_lines:])
    except OSError:
        return ""


def _live_dir(results_dir: Path, legacy_id: str) -> Path:
    return Path(results_dir) / legacy_id / ".live"


def any_task_running(session_id) -> bool:
    """``True`` if any of the session's jobs is in a running-ish state.

    DB-driven so it survives hard browser refreshes (unlike the
    ``st.session_state``-backed ``panels._shared.any_panel_task_running``,
    which only sees tasks this browser session originated).
    """
    if session_id is None:
        return False
    try:
        from failure_view import load_status_for_session
    except Exception:
        return False
    for row in load_status_for_session(session_id) or []:
        if row.get("status") in _RUNNING_STATUSES:
            return True
    return False


def find_active_stage_log(
    session_id,
    results_dir: Path,
    *,
    prefer_stdout: bool = True,
) -> Optional[Path]:
    """Locate the most-recent running job's most-recent stage log file.

    Walks the session's running jobs (newest-first by ``last_updated``),
    looks under ``results/<legacy_id>/.live/`` for ``*.stdout.log``
    files, returns the most-recently-modified one. ``None`` when no
    running job has produced a log yet.

    When ``prefer_stdout`` is false the helper picks ``*.stderr.log``
    instead — useful if a downstream caller wants to surface a
    differentiated error stream.
    """
    if session_id is None:
        return None
    try:
        from failure_view import load_status_for_session
    except Exception:
        return None

    suffix = ".stdout.log" if prefer_stdout else ".stderr.log"
    candidates: list[Path] = []
    for row in load_status_for_session(session_id) or []:
        if row.get("status") not in _RUNNING_STATUSES:
            continue
        legacy_id = row.get("legacy_id")
        if not legacy_id:
            continue
        live_dir = _live_dir(results_dir, legacy_id)
        if not live_dir.exists():
            continue
        for log in live_dir.glob(f"*{suffix}"):
            candidates.append(log)
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None


def list_active_running_jobs(session_id) -> list[dict]:
    """All running-ish jobs for the session, newest-first.

    Used by the live-log fragment header to label the log block with
    the active job's kind + legacy_id.
    """
    if session_id is None:
        return []
    try:
        from failure_view import load_status_for_session
    except Exception:
        return []
    rows = [
        r for r in (load_status_for_session(session_id) or [])
        if r.get("status") in _RUNNING_STATUSES
    ]
    rows.sort(key=lambda r: r.get("last_updated") or "", reverse=True)
    return rows
