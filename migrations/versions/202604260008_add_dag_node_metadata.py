"""add DAG node metadata

Revision ID: 202604260008
Revises: 202604260007
Create Date: 2026-04-26 00:00:08.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from agentic_sdlc_platform.persistence.models import JsonDocument

revision: str = "202604260008"
down_revision: str | None = "202604260007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_dag_nodes",
        sa.Column("metadata_json", JsonDocument(), nullable=False, server_default="{}"),
    )
    op.alter_column("task_dag_nodes", "metadata_json", server_default=None)


def downgrade() -> None:
    op.drop_column("task_dag_nodes", "metadata_json")
