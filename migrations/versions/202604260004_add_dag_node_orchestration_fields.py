"""add DAG node orchestration fields

Revision ID: 202604260004
Revises: 202604260003
Create Date: 2026-04-26 00:00:04.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202604260004"
down_revision: str | None = "202604260003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("task_dag_nodes", sa.Column("orchestrator_task_id", sa.String(), nullable=True))
    op.add_column("task_dag_nodes", sa.Column("orchestrator_status", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("task_dag_nodes", "orchestrator_status")
    op.drop_column("task_dag_nodes", "orchestrator_task_id")
