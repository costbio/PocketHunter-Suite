"""Defensive insert helper for the ``audit_events`` table (B3.4).

Single function: ``record(session_id, ip, action, details=None)``.
Designed to be called from any user-facing mutation site without
risking that an audit failure blocks the user's request — every
exception is swallowed and logged.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from db.models import AuditEvent
from db.session import get_db

log = logging.getLogger(__name__)


def record(
    session_id: Optional[uuid.UUID],
    ip: Optional[str],
    action: str,
    details: Optional[dict[str, Any]] = None,
) -> None:
    """Insert one audit row. Never raises.

    Callers stay simple: ``audit.record(session.id, client_ip(),
    "find_pockets_submit", {"job_id": job_id})``. If the DB hiccups
    the only side-effect is a single warning log line.
    """
    try:
        with get_db() as db:
            row = AuditEvent(
                session_id=session_id,
                ip=(ip or None),
                action=action,
                details=details or None,
            )
            db.add(row)
    except Exception as e:
        # Audit failures must not surface to the user. Log and move on.
        log.warning("audit_event insert failed (%s): %s", action, e)
