"""add orchestrator fields to agent sessions

Revision ID: 202604260010
Revises: 202604260009
Create Date: 2026-04-26 00:00:10.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202604260010"
down_revision: str | None = "202604260009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_sessions", sa.Column("orchestrator_provider", sa.String(), nullable=True))
    op.add_column("agent_sessions", sa.Column("orchestrator_issue_id", sa.String(), nullable=True))
    op.add_column("agent_sessions", sa.Column("orchestrator_task_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_sessions", "orchestrator_task_id")
    op.drop_column("agent_sessions", "orchestrator_issue_id")
    op.drop_column("agent_sessions", "orchestrator_provider")
