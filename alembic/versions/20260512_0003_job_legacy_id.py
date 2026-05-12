"""jobs.legacy_id — bridge column for v1 disk job_ids

Revision ID: 0003_job_legacy_id
Revises: 0002_session_and_job
Create Date: 2026-05-12 00:00:02.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003_job_legacy_id"
down_revision: Union[str, Sequence[str], None] = "0002_session_and_job"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("legacy_id", sa.String(96), nullable=True),
    )
    op.create_index("ix_jobs_legacy_id", "jobs", ["legacy_id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_legacy_id", table_name="jobs")
    op.drop_column("jobs", "legacy_id")
