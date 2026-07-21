"""Persist normalized Jenkins source attribution.

Revision ID: 0006_source_attribution
Revises: 0005_analysis_costs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_source_attribution"
down_revision: str | None = "0005_analysis_costs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.add_column(
        "jenkins_builds",
        sa.Column("source_kind", sa.String(length=32), server_default="unresolved", nullable=False),
    )
    op.add_column(
        "jenkins_builds",
        sa.Column("source_status", sa.String(length=32), server_default="pending", nullable=False),
    )
    op.add_column("jenkins_builds", sa.Column("source_profile_id", sa.String(length=128), nullable=True))
    op.add_column("jenkins_builds", sa.Column("source_branch", sa.String(length=768), nullable=True))
    op.add_column("jenkins_builds", sa.Column("source_commit_sha", sa.String(length=128), nullable=True))
    op.add_column("jenkins_builds", sa.Column("source_url", sa.String(length=2048), nullable=True))
    op.add_column("jenkins_builds", sa.Column("source_title", sa.String(length=1024), nullable=True))
    op.add_column("jenkins_builds", sa.Column("source_state", sa.String(length=64), nullable=True))
    op.add_column(
        "jenkins_builds",
        sa.Column("source_resolution_method", sa.String(length=64), server_default="none", nullable=False),
    )
    op.add_column("jenkins_builds", sa.Column("source_reason", sa.String(length=512), nullable=True))
    op.add_column(
        "jenkins_builds",
        sa.Column("source_allow_mr_comments", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "jenkins_builds",
        sa.Column("source_provenance", JSON, server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.add_column("jenkins_builds", sa.Column("source_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jenkins_builds", sa.Column("source_attributed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_jenkins_builds_source_status", "jenkins_builds", ["source_status", "started_at"])
    op.execute(
        "UPDATE jenkins_builds SET source_status = 'not_needed' "
        "WHERE result NOT IN ('FAILURE', 'UNSTABLE', 'ABORTED')"
    )


def downgrade() -> None:
    op.drop_index("ix_jenkins_builds_source_status", table_name="jenkins_builds")
    op.drop_column("jenkins_builds", "source_attributed_at")
    op.drop_column("jenkins_builds", "source_verified_at")
    op.drop_column("jenkins_builds", "source_provenance")
    op.drop_column("jenkins_builds", "source_allow_mr_comments")
    op.drop_column("jenkins_builds", "source_reason")
    op.drop_column("jenkins_builds", "source_resolution_method")
    op.drop_column("jenkins_builds", "source_state")
    op.drop_column("jenkins_builds", "source_title")
    op.drop_column("jenkins_builds", "source_url")
    op.drop_column("jenkins_builds", "source_commit_sha")
    op.drop_column("jenkins_builds", "source_branch")
    op.drop_column("jenkins_builds", "source_profile_id")
    op.drop_column("jenkins_builds", "source_status")
    op.drop_column("jenkins_builds", "source_kind")
