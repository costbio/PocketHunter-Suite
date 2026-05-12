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
    db: Optional[OrmSession] = None,
) -> Job:
    """Insert a new job row, status starts at 'submitted'."""
    row = Job(
        session_id=session_id,
        kind=kind,
        celery_task_id=celery_task_id,
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
