"""CRUD helpers for the ``jobs`` table.

Phase A2 lays the helpers; phase A3 wires the existing
``tasks._update_status_file`` + ``_fail_job`` call sites through them.

The helpers mirror the JSON shape that today's status files already carry
(``status``, ``step``, ``task_id``, ``result_info``, ``error``,
``pair_failures``, ``pair_failures_log``) so A3 is a pivot, not a rewrite.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from db.models import Job
from db.session import get_db


def create_job(
    *,
    session_id: uuid.UUID,
    kind: str,
    celery_task_id: Optional[str] = None,
    legacy_id: Optional[str] = None,
    db: Optional[OrmSession] = None,
) -> Job:
    """Insert a new job row, status starts at 'submitted'.

    ``legacy_id`` is the v1 disk-style string job_id (e.g.
    ``find_pockets_20260512_120000_abcd1234``). Passing it lets the task
    layer find this row when it writes status updates.
    """
    row = Job(
        session_id=session_id,
        kind=kind,
        celery_task_id=celery_task_id,
        legacy_id=legacy_id,
        status="submitted",
    )
    if db is None:
        with get_db() as inner_db:
            inner_db.add(row)
            inner_db.flush()
            inner_db.refresh(row)
            inner_db.expunge(row)
        return row
    db.add(row)
    db.flush()
    db.refresh(row)
    return row


def create_for_legacy(
    session_id: uuid.UUID,
    kind: str,
    legacy_id: str,
    *,
    celery_task_id: Optional[str] = None,
    db: Optional[OrmSession] = None,
) -> Job:
    """Convenience: create a Job row tagged with a v1 disk-style ``legacy_id``.

    Use at submission time on the page::

        from db.jobs import create_for_legacy
        if current_session is not None:
            create_for_legacy(current_session.id, "find_pockets", job_id)
        task.delay(..., job_id=job_id)  # task signature unchanged
    """
    return create_job(
        session_id=session_id,
        kind=kind,
        celery_task_id=celery_task_id,
        legacy_id=legacy_id,
        db=db,
    )


def find_by_legacy_id(legacy_id: str, *, db: Optional[OrmSession] = None) -> Optional[Job]:
    """Look up a Job by its v1 disk-style ``legacy_id``."""
    stmt = select(Job).where(Job.legacy_id == legacy_id)
    if db is None:
        with get_db() as inner_db:
            row = inner_db.scalars(stmt).one_or_none()
            if row is not None:
                inner_db.expunge(row)
            return row
    return db.scalars(stmt).one_or_none()


def update_status(
    job_id: uuid.UUID,
    status: str,
    *,
    step: Optional[str] = None,
    celery_task_id: Optional[str] = None,
    result_info: Optional[dict] = None,
    error: Optional[dict] = None,
    pair_failures: Optional[list] = None,
    pair_failures_log: Optional[str] = None,
    db: Optional[OrmSession] = None,
) -> None:
    """Update a job row in-place. Any None argument is left alone."""
    def _do(db_: OrmSession) -> None:
        row = db_.get(Job, job_id)
        if row is None:
            raise LookupError(f"Job {job_id} not found")
        row.status = status
        if step is not None:
            row.step = step
        if celery_task_id is not None:
            row.celery_task_id = celery_task_id
        if result_info is not None:
            row.result_info = result_info
        if error is not None:
            row.error = error
        if pair_failures is not None:
            row.pair_failures = pair_failures
        if pair_failures_log is not None:
            row.pair_failures_log = pair_failures_log

    if db is None:
        with get_db() as inner_db:
            _do(inner_db)
    else:
        _do(db)


def update_by_legacy_id(
    legacy_id: str,
    status: str,
    *,
    step: Optional[str] = None,
    celery_task_id: Optional[str] = None,
    result_info: Optional[dict] = None,
    error: Optional[dict] = None,
    pair_failures: Optional[list] = None,
    pair_failures_log: Optional[str] = None,
    db: Optional[OrmSession] = None,
) -> bool:
    """Update a Job row keyed by ``legacy_id``.

    Returns ``True`` if a row was found and updated, ``False`` otherwise.
    Silent no-op on miss — used by the task layer's mirror-to-DB path so a
    task running before/without a registered Job row doesn't crash.
    """
    def _do(db_: OrmSession) -> bool:
        row = db_.scalars(select(Job).where(Job.legacy_id == legacy_id)).one_or_none()
        if row is None:
            return False
        row.status = status
        if step is not None:
            row.step = step
        if celery_task_id is not None:
            row.celery_task_id = celery_task_id
        if result_info is not None:
            row.result_info = result_info
        if error is not None:
            row.error = error
        if pair_failures is not None:
            row.pair_failures = pair_failures
        if pair_failures_log is not None:
            row.pair_failures_log = pair_failures_log
        return True

    if db is None:
        with get_db() as inner_db:
            return _do(inner_db)
    return _do(db)


def find_by_session(
    session_id: uuid.UUID,
    *,
    kind: Optional[str] = None,
    db: Optional[OrmSession] = None,
) -> list[Job]:
    """Return jobs for a session, optionally filtered to a single ``kind``,
    ordered newest-first."""
    stmt = select(Job).where(Job.session_id == session_id).order_by(Job.created_at.desc())
    if kind is not None:
        stmt = stmt.where(Job.kind == kind)
    if db is None:
        with get_db() as inner_db:
            rows = list(inner_db.scalars(stmt).all())
            for r in rows:
                inner_db.expunge(r)
            return rows
    return list(db.scalars(stmt).all())


def get(job_id: uuid.UUID, *, db: Optional[OrmSession] = None) -> Optional[Job]:
    """Fetch a single job by ID, or None."""
    if db is None:
        with get_db() as inner_db:
            row = inner_db.get(Job, job_id)
            if row is not None:
                inner_db.expunge(row)
            return row
    return db.get(Job, job_id)
