"""create repo index jobs

Revision ID: 202604260007
Revises: 202604260006
Create Date: 2026-04-26 00:00:07.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from agentic_sdlc_platform.persistence.models import JsonDocument

revision: str = "202604260007"
down_revision: str | None = "202604260006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repo_index_jobs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("repo_name", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("external_index_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("metadata_json", JsonDocument(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_repo_index_jobs_repo_status",
        "repo_index_jobs",
        ["repo_name", "status"],
    )
    op.create_index(
        "ix_repo_index_jobs_external_index_id",
        "repo_index_jobs",
        ["external_index_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_repo_index_jobs_external_index_id", table_name="repo_index_jobs")
    op.drop_index("ix_repo_index_jobs_repo_status", table_name="repo_index_jobs")
    op.drop_table("repo_index_jobs")
