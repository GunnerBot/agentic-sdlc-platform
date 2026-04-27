"""create DAG node execution records

Revision ID: 202604260009
Revises: 202604260008
Create Date: 2026-04-26 00:00:09.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from agentic_sdlc_platform.persistence.models import JsonDocument

revision: str = "202604260009"
down_revision: str | None = "202604260008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dag_node_executions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("dag_id", sa.String(), nullable=False),
        sa.Column("node_key", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("executor_provider", sa.String(), nullable=False),
        sa.Column("external_execution_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("branch_name", sa.String(), nullable=True),
        sa.Column("pr_url", sa.String(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("workspace_path", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("metadata_json", JsonDocument(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["dag_id"], ["task_dags.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dag_node_executions_dag_node",
        "dag_node_executions",
        ["dag_id", "node_key"],
    )
    op.create_index(
        "ix_dag_node_executions_status",
        "dag_node_executions",
        ["status"],
    )
    op.create_index(
        "ix_dag_node_executions_external",
        "dag_node_executions",
        ["external_execution_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_dag_node_executions_external", table_name="dag_node_executions")
    op.drop_index("ix_dag_node_executions_status", table_name="dag_node_executions")
    op.drop_index("ix_dag_node_executions_dag_node", table_name="dag_node_executions")
    op.drop_table("dag_node_executions")
