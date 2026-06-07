"""audit_events — forensic record of editor mutations (B3.4)

Revision ID: 0004_audit_events
Revises: 0003_job_legacy_id
Create Date: 2026-05-16 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = "0004_audit_events"
down_revision: Union[str, Sequence[str], None] = "0003_job_legacy_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id", UUID(as_uuid=True),
            sa.ForeignKey("sessions.id", ondelete="SET NULL"),
            nullable=True, index=True,
        ),
        sa.Column("ip", sa.String(45), nullable=True, index=True),
        sa.Column("action", sa.String(64), nullable=False, index=True),
        sa.Column("details", JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False, index=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("audit_events")
