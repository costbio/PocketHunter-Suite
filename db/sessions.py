"""CRUD helpers for the ``sessions`` table.

Public surface (all functions are short and import-light):

* ``create_session(display_name=None, state=None)`` — make a new row, return it.
* ``load_session(short_code)`` — fetch by short_code, or ``None``.
* ``is_editor(session, edit_secret)`` — compare-equals the secret.
* ``touch_last_active(session)`` — bump ``last_active_at``.
* ``mark_expired(session)`` — set ``expired_at``; soft-expiry per the strategy.
* ``new_short_code()`` / ``new_edit_secret()`` — exported so callers can
  regenerate or check uniqueness if needed.

Functions accept an optional ``db: Session`` (SQLAlchemy session). If omitted
they open a short-lived one via ``db.session.get_db()`` so the call sites can
stay terse.
"""
from __future__ import annotations

import datetime as _dt
import os
import secrets
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from db.models import Session as SessionRow
from db.session import get_db


# 8 bytes of url-safe base64 ≈ 11 chars. Collision-resistant for the
# foreseeable session volume (collisions before ~2.8e9 sessions).
_SHORT_CODE_BYTES = 8
# 24 bytes ≈ 32 chars. Plenty for an anonymous edit token.
_EDIT_SECRET_BYTES = 24


def new_short_code() -> str:
    """Generate a fresh URL-safe short_code that's safe to embed in
    Streamlit bidirectional component keys.

    Streamlit's CCv2 validator rejects keys containing ``__``.
    ``secrets.token_urlsafe`` uses ``-`` and ``_`` as the URL-safe
    base64 substitutes, so we must reject candidates with ``__`` AND
    candidates that start/end with ``_`` — the latter would otherwise
    produce ``__`` once concatenated as ``f"prefix_{short_code}"`` at
    a component-key call site. Retry until the candidate is clean;
    each constraint trims ~1/256 of the draw space, so the loop
    terminates in expectation in ≪10 iterations.
    """
    while True:
        candidate = secrets.token_urlsafe(_SHORT_CODE_BYTES)
        if (
            "__" not in candidate
            and not candidate.startswith("_")
            and not candidate.endswith("_")
        ):
            return candidate


def safe_bidi_short(code: str) -> str:
    """Normalise a session short_code into a form safe for embedding in
    Streamlit bidirectional component keys.

    Honours the CCv2 validator's "no ``__``" rule across both
    pre-existing ``__`` substrings AND ``__`` introduced by f-string
    concatenation (e.g. ``f"viewer_{code}"`` when ``code`` starts with
    ``_``). Uniqueness is preserved by padding boundary underscores
    with the literal letter ``s`` rather than stripping — two codes
    that differ only in a leading/trailing ``_`` still map to distinct
    safe forms.

    New short_codes from :func:`new_short_code` are already free of
    every condition this function handles; the helper exists to keep
    legacy codes in the DB (created before the generator was tightened)
    working without a data migration.
    """
    safe = code
    # Collapse any run of underscores to a single one — handles legacy
    # ``__`` and the theoretical ``___``. ``replace("__", "_")`` would
    # only collapse one pass, so loop until idempotent.
    while "__" in safe:
        safe = safe.replace("__", "_")
    if safe.startswith("_"):
        safe = "s" + safe
    if safe.endswith("_"):
        safe = safe + "s"
    return safe


def new_edit_secret() -> str:
    return secrets.token_urlsafe(_EDIT_SECRET_BYTES)


def create_session(
    *,
    display_name: Optional[str] = None,
    state: Optional[dict] = None,
    db: Optional[OrmSession] = None,
) -> SessionRow:
    """Create a new session row with fresh short_code + edit_secret."""
    row = SessionRow(
        short_code=new_short_code(),
        edit_secret=new_edit_secret(),
        display_name=display_name,
        state=state or {},
    )
    if db is None:
        with get_db() as inner_db:
            inner_db.add(row)
            inner_db.flush()
            inner_db.refresh(row)
            # Detach so the caller can use the row after the context closes.
            inner_db.expunge(row)
        return row
    db.add(row)
    db.flush()
    db.refresh(row)
    return row


def load_session(short_code: str, *, db: Optional[OrmSession] = None) -> Optional[SessionRow]:
    """Look up a session by short_code. Returns None if not found."""
    stmt = select(SessionRow).where(SessionRow.short_code == short_code)
    if db is None:
        with get_db() as inner_db:
            row = inner_db.scalars(stmt).one_or_none()
            if row is not None:
                inner_db.expunge(row)
            return row
    return db.scalars(stmt).one_or_none()


def is_editor(session: SessionRow, edit_secret: Optional[str]) -> bool:
    """Constant-time-ish equality check on the edit secret."""
    if not edit_secret:
        return False
    return secrets.compare_digest(session.edit_secret, edit_secret)


def is_expired(session: SessionRow) -> bool:
    """``True`` if the session has been soft-expired."""
    return session.expired_at is not None


def touch_last_active(session: SessionRow, *, db: Optional[OrmSession] = None) -> None:
    """Bump ``last_active_at`` to now."""
    now = _dt.datetime.now(_dt.timezone.utc)
    if db is None:
        with get_db() as inner_db:
            inner_db.merge(SessionRow(id=session.id, last_active_at=now))
        session.last_active_at = now
        return
    session.last_active_at = now
    db.flush()


def disk_usage_mb(session_id, *, db: Optional[OrmSession] = None) -> float:
    """Sum results/<legacy_id>/ and uploads/<legacy_id>/ bytes for the session.

    Phase C C4 quota check: callers compare this against
    ``settings.PER_SESSION_DISK_QUOTA_MB`` before allowing an upload.

    Walks the disk rather than reading file-size columns out of the DB —
    the schema doesn't track per-file size today, and the disk-walk is
    fast enough (one ``os.scandir`` per job, jobs-per-session is small).
    Returns megabytes (float). Missing dirs contribute 0; never raises.
    """
    from db import jobs as _jobs_repo
    from settings import settings

    def _dir_bytes(path) -> int:
        total = 0
        try:
            for root, _dirs, files in os.walk(path):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        total += os.path.getsize(fp)
                    except OSError:
                        # File raced out from under us; ignore.
                        pass
        except OSError:
            pass
        return total

    rows = _jobs_repo.find_by_session(session_id, db=db)
    bytes_total = 0
    for job in rows:
        if not job.legacy_id:
            continue
        bytes_total += _dir_bytes(settings.RESULTS_DIR / job.legacy_id)
        bytes_total += _dir_bytes(settings.UPLOAD_DIR / job.legacy_id)
    return bytes_total / (1024 * 1024)


def pinned_job_legacy_ids(*, db: Optional[OrmSession] = None) -> set[str]:
    """Disk job-ids belonging to pinned sessions.

    ``ResourceManager.cleanup_old_jobs`` walks the uploads and results
    directories and deletes by mtime alone — it has no view of the
    database, and keeping it that way is deliberate (it is a filesystem
    utility, usable on a box with no DB reachable). So the protection
    set is computed here and handed to it, rather than teaching it to
    query.

    Returns an empty set on any DB error. That is the safe direction for
    a *quota* check but the unsafe one here, since an empty set means
    "protect nothing" — callers that cannot tolerate losing a pinned
    session's artefacts should treat a DB outage as a reason to skip the
    sweep entirely, which ``cleanup_old_jobs_task`` does.
    """
    from db.models import Job, Session as _SessionRow

    stmt = (
        select(Job.legacy_id)
        .join(_SessionRow, Job.session_id == _SessionRow.id)
        .where(_SessionRow.pinned.is_(True), Job.legacy_id.is_not(None))
    )
    if db is None:
        with get_db() as inner_db:
            return {r for r in inner_db.scalars(stmt).all() if r}
    return {r for r in db.scalars(stmt).all() if r}


def mark_expired(session: SessionRow, *, db: Optional[OrmSession] = None) -> None:
    """Set ``expired_at`` to now (idempotent)."""
    if session.expired_at is not None:
        return
    now = _dt.datetime.now(_dt.timezone.utc)
    if db is None:
        with get_db() as inner_db:
            attached = inner_db.merge(session)
            attached.expired_at = now
        session.expired_at = now
        return
    session.expired_at = now
    db.flush()
