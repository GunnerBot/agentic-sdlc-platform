"""create repository registry

Revision ID: 202604260006
Revises: 202604260005
Create Date: 2026-04-26 00:00:06.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from agentic_sdlc_platform.persistence.models import JsonDocument

revision: str = "202604260006"
down_revision: str | None = "202604260005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("clone_url", sa.String(), nullable=True),
        sa.Column("default_branch", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("metadata_json", JsonDocument(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_repositories_name"),
    )
    op.create_index(
        "ix_repositories_provider_status",
        "repositories",
        ["provider", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_repositories_provider_status", table_name="repositories")
    op.drop_table("repositories")
