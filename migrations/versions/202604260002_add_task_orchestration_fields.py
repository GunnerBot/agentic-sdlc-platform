"""add task orchestration fields

Revision ID: 202604260002
Revises: 202604260001
Create Date: 2026-04-26 00:00:02.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202604260002"
down_revision: str | None = "202604260001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("orchestrator_task_id", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("orchestrator_status", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "orchestrator_status")
    op.drop_column("tasks", "orchestrator_task_id")
