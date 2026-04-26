"""create task DAG tables

Revision ID: 202604260003
Revises: 202604260002
Create Date: 2026-04-26 00:00:03.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from agentic_sdlc_platform.persistence.models import JsonDocument

revision: str = "202604260003"
down_revision: str | None = "202604260002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_dags",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_dags_task_id", "task_dags", ["task_id"])
    op.create_table(
        "task_dag_nodes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("dag_id", sa.String(), nullable=False),
        sa.Column("node_key", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("repo", sa.String(), nullable=True),
        sa.Column("depends_on_json", JsonDocument(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["dag_id"], ["task_dags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dag_id", "node_key", name="uq_task_dag_nodes_dag_node_key"),
    )
    op.create_index(
        "ix_task_dag_nodes_dag_status",
        "task_dag_nodes",
        ["dag_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_dag_nodes_dag_status", table_name="task_dag_nodes")
    op.drop_table("task_dag_nodes")
    op.drop_index("ix_task_dags_task_id", table_name="task_dags")
    op.drop_table("task_dags")
