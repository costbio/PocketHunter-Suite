"""sessions + jobs tables

Revision ID: 0002_session_and_job
Revises: 0001_baseline
Create Date: 2026-05-12 00:00:01.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0002_session_and_job"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid_type():
    """Portable UUID — Uuid on Postgres, CHAR(36) on SQLite."""
    return sa.Uuid(as_uuid=True)


def _jsonb_type():
    """JSONB on Postgres, JSON on SQLite."""
    return JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", _uuid_type(), primary_key=True),
        sa.Column("short_code", sa.String(16), nullable=False),
        sa.Column("edit_secret", sa.String(48), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_id", _uuid_type(), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("state", _jsonb_type(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_index("ix_sessions_short_code", "sessions", ["short_code"], unique=True)

    op.create_table(
        "jobs",
        sa.Column("id", _uuid_type(), primary_key=True),
        sa.Column(
            "session_id",
            _uuid_type(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="submitted"),
        sa.Column("step", sa.String(255), nullable=True),
        sa.Column("celery_task_id", sa.String(64), nullable=True),
        sa.Column("result_info", _jsonb_type(), nullable=True),
        sa.Column("error", _jsonb_type(), nullable=True),
        sa.Column("pair_failures", _jsonb_type(), nullable=True),
        sa.Column("pair_failures_log", sa.String(512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_jobs_session_id", "jobs", ["session_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_jobs_session_id", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_sessions_short_code", table_name="sessions")
    op.drop_table("sessions")
