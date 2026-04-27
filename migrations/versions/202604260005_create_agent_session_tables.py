"""create agent session tables

Revision ID: 202604260005
Revises: 202604260004
Create Date: 2026-04-26 00:00:05.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from agentic_sdlc_platform.persistence.models import JsonDocument

revision: str = "202604260005"
down_revision: str | None = "202604260004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("external_thread_id", sa.String(), nullable=False),
        sa.Column("hermes_session_id", sa.String(), nullable=True),
        sa.Column("repo", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("context_summary", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "external_thread_id",
            name="uq_agent_sessions_provider_thread",
        ),
    )
    op.create_index("ix_agent_sessions_task_id", "agent_sessions", ["task_id"])
    op.create_index("ix_agent_sessions_status", "agent_sessions", ["status"])
    op.create_table(
        "session_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=True),
        sa.Column("metadata_json", JsonDocument(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["agent_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_session_events_session_id", "session_events", ["session_id"])
    op.create_index("ix_session_events_type", "session_events", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_session_events_type", table_name="session_events")
    op.drop_index("ix_session_events_session_id", table_name="session_events")
    op.drop_table("session_events")
    op.drop_index("ix_agent_sessions_status", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_task_id", table_name="agent_sessions")
    op.drop_table("agent_sessions")
