"""Add durable Jenkins failure reports.

Revision ID: 0008_jenkins_failure_reports
Revises: 0007_scan_analysis_indexes
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_jenkins_failure_reports"
down_revision: str | None = "0007_scan_analysis_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "jenkins_failure_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True)),
        sa.Column("jobs_discovered", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failures_found", sa.Integer(), server_default="0", nullable=False),
        sa.Column("coverage_exceptions", JSON, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("budget_reset_at", sa.DateTime(timezone=True)),
        sa.Column("error_summary", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jenkins_failure_reports_created", "jenkins_failure_reports", ["created_at", "id"])
    op.create_table(
        "jenkins_failure_report_builds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("build_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="queued", nullable=False),
        sa.Column("investigation_request_id", sa.Uuid()),
        sa.Column("error_summary", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["report_id"], ["jenkins_failure_reports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["build_id"], ["jenkins_builds.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["investigation_request_id"], ["investigation_requests.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("report_id", "build_id", name="uq_jenkins_failure_report_build"),
    )
    op.create_index("ix_jenkins_failure_report_builds_report_status", "jenkins_failure_report_builds", ["report_id", "status", "id"])


def downgrade() -> None:
    op.drop_index("ix_jenkins_failure_report_builds_report_status", table_name="jenkins_failure_report_builds")
    op.drop_table("jenkins_failure_report_builds")
    op.drop_index("ix_jenkins_failure_reports_created", table_name="jenkins_failure_reports")
    op.drop_table("jenkins_failure_reports")
