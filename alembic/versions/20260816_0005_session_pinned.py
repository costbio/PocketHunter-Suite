"""sessions.pinned — exempt a session from every cleanup sweep

Revision ID: 0005_session_pinned
Revises: 0004_audit_events
Create Date: 2026-08-16 00:00:00.000000

Pinning exists so the published demo session survives indefinitely. A
paper cites its URL, so it has to outlive CLEANUP_AFTER_DAYS, the
per-session disk quota and the abandoned-session reaper alike.

``server_default`` is set so the column backfills to false on existing
rows without a separate UPDATE, and so inserts from code paths that
predate this column keep working.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0005_session_pinned"
down_revision: Union[str, Sequence[str], None] = "0004_audit_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "pinned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("sessions", "pinned")
