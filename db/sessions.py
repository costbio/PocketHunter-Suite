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
    """Generate a fresh URL-safe short_code that avoids Streamlit's
    bidi-component key delimiter (``__``).

    ``secrets.token_urlsafe`` uses ``-`` and ``_`` as the URL-safe
    base64 substitutes, so a roughly 1-in-256 chance of a ``__`` slips
    through per draw. When that happens, retry until we get a clean
    code — the loop terminates in expectation in ≪10 iterations.
    """
    while True:
        candidate = secrets.token_urlsafe(_SHORT_CODE_BYTES)
        if "__" not in candidate:
            return candidate


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
