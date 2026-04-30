"""create workspace github installation records

Revision ID: 202604300001
Revises: 202604260011
Create Date: 2026-04-30 00:00:01.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from agentic_sdlc_platform.persistence.models import JsonDocument

revision: str = "202604300001"
down_revision: str | None = "202604260011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_github_installations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("installation_id", sa.String(), nullable=False),
        sa.Column("account", sa.String(), nullable=True),
        sa.Column("repository_selection", sa.String(), nullable=False),
        sa.Column("permissions_json", JsonDocument(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("metadata_json", JsonDocument(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "installation_id",
            name="uq_workspace_github_installations_workspace_installation",
        ),
    )
    op.create_index(
        "ix_workspace_github_installations_workspace_status",
        "workspace_github_installations",
        ["workspace_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_github_installations_workspace_status",
        table_name="workspace_github_installations",
    )
    op.drop_table("workspace_github_installations")
