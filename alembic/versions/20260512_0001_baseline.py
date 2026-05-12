"""baseline — no-op revision establishing the alembic_version table.

Phase A commit A1. Subsequent migrations (Session + Job tables land in A2)
chain from this baseline.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-12 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Baseline is intentionally empty — creating the alembic_version row is
    a side effect of running this migration. Real tables ship in A2."""
    pass


def downgrade() -> None:
    pass
