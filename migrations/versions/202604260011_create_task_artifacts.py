"""create task artifact records

Revision ID: 202604260011
Revises: 202604260010
Create Date: 2026-04-26 00:00:11.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from agentic_sdlc_platform.persistence.models import JsonDocument

revision: str = "202604260011"
down_revision: str | None = "202604260010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_artifacts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("dag_id", sa.String(), nullable=True),
        sa.Column("node_key", sa.String(), nullable=True),
        sa.Column("execution_id", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("content_json", JsonDocument(), nullable=False),
        sa.Column("metadata_json", JsonDocument(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["dag_id"], ["task_dags.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["dag_node_executions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_task_artifacts_task_kind",
        "task_artifacts",
        ["task_id", "kind"],
    )
    op.create_index(
        "ix_task_artifacts_dag_node",
        "task_artifacts",
        ["dag_id", "node_key"],
    )
    op.create_index(
        "ix_task_artifacts_execution",
        "task_artifacts",
        ["execution_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_artifacts_execution", table_name="task_artifacts")
    op.drop_index("ix_task_artifacts_dag_node", table_name="task_artifacts")
    op.drop_index("ix_task_artifacts_task_kind", table_name="task_artifacts")
    op.drop_table("task_artifacts")
