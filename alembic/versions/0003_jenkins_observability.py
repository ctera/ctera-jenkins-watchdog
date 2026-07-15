"""Durable Jenkins job and build observability.

Revision ID: 0003_jenkins_observability
Revises: 0002_operational_overview
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_jenkins_observability"
down_revision: str | None = "0002_operational_overview"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "jenkins_jobs",
        sa.Column("full_name", sa.String(length=768), nullable=False),
        sa.Column("display_name", sa.String(length=512), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("job_class", sa.String(length=512), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("color", sa.String(length=32), nullable=True),
        sa.Column("parent_full_name", sa.String(length=768), nullable=True),
        sa.Column("first_build_number", sa.Integer(), nullable=True),
        sa.Column("first_build_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_build_number", sa.Integer(), nullable=True),
        sa.Column("last_build_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("watermark_build_number", sa.Integer(), nullable=True),
        sa.Column("history_coverage", sa.String(length=40), server_default="unknown", nullable=False),
        sa.Column("head_type", sa.String(length=32), server_default="unknown", nullable=False),
        sa.Column("head_name", sa.String(length=768), nullable=True),
        sa.Column("source_provider", sa.String(length=32), nullable=True),
        sa.Column("repository", sa.String(length=768), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("full_name"),
    )
    op.create_index("ix_jenkins_jobs_parent", "jenkins_jobs", ["parent_full_name"])
    op.create_index("ix_jenkins_jobs_last_build", "jenkins_jobs", ["last_build_at"])
    op.create_index("ix_jenkins_jobs_type", "jenkins_jobs", ["job_type", "head_type"])

    op.create_table(
        "jenkins_builds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_full_name", sa.String(length=768), nullable=False),
        sa.Column("build_number", sa.Integer(), nullable=False),
        sa.Column("result", sa.String(length=24), nullable=False),
        sa.Column("building", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("upstream_job_full_name", sa.String(length=768), nullable=True),
        sa.Column("upstream_build_number", sa.Integer(), nullable=True),
        sa.Column("root_job_full_name", sa.String(length=768), nullable=True),
        sa.Column("root_build_number", sa.Integer(), nullable=True),
        sa.Column("logical_run_key", sa.String(length=1024), nullable=False),
        sa.Column("trigger_kind", sa.String(length=64), server_default="unknown", nullable=False),
        sa.Column("source_provider", sa.String(length=32), nullable=True),
        sa.Column("repository", sa.String(length=768), nullable=True),
        sa.Column("change_number", sa.String(length=128), nullable=True),
        sa.Column("change_url", sa.String(length=2048), nullable=True),
        sa.Column("head_name", sa.String(length=768), nullable=True),
        sa.Column("failed_stage", sa.String(length=768), nullable=True),
        sa.Column("failure_classification", sa.String(length=64), server_default="unknown", nullable=False),
        sa.Column("failure_signature", sa.String(length=64), server_default="", nullable=False),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("propagated_failure", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("novelty", sa.String(length=32), server_default="unclassified", nullable=False),
        sa.Column("priority_score", sa.Integer(), server_default="0", nullable=False),
        sa.Column("priority_reasons", JSON, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("evidence", JSON, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("enrichment_status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_full_name"], ["jenkins_jobs.full_name"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_full_name", "build_number", name="uq_jenkins_build_job_number"),
    )
    op.create_index("ix_jenkins_builds_started", "jenkins_builds", ["started_at", "id"])
    op.create_index("ix_jenkins_builds_result_started", "jenkins_builds", ["result", "started_at"])
    op.create_index("ix_jenkins_builds_logical_run", "jenkins_builds", ["logical_run_key"])
    op.create_index("ix_jenkins_builds_signature", "jenkins_builds", ["failure_signature", "started_at"])
    op.create_index("ix_jenkins_builds_enrichment", "jenkins_builds", ["enrichment_status", "started_at"])

    op.create_table(
        "jenkins_build_edges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("upstream_build_id", sa.Uuid(), nullable=False),
        sa.Column("downstream_build_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.String(length=32), server_default="triggered", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["downstream_build_id"], ["jenkins_builds.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["upstream_build_id"], ["jenkins_builds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("upstream_build_id", "downstream_build_id", name="uq_jenkins_build_edge"),
    )
    op.create_index("ix_jenkins_build_edges_downstream", "jenkins_build_edges", ["downstream_build_id"])

    op.create_table(
        "jenkins_sync_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("stats", JSON, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("jenkins_sync_state")
    op.drop_index("ix_jenkins_build_edges_downstream", table_name="jenkins_build_edges")
    op.drop_table("jenkins_build_edges")
    op.drop_index("ix_jenkins_builds_enrichment", table_name="jenkins_builds")
    op.drop_index("ix_jenkins_builds_signature", table_name="jenkins_builds")
    op.drop_index("ix_jenkins_builds_logical_run", table_name="jenkins_builds")
    op.drop_index("ix_jenkins_builds_result_started", table_name="jenkins_builds")
    op.drop_index("ix_jenkins_builds_started", table_name="jenkins_builds")
    op.drop_table("jenkins_builds")
    op.drop_index("ix_jenkins_jobs_type", table_name="jenkins_jobs")
    op.drop_index("ix_jenkins_jobs_last_build", table_name="jenkins_jobs")
    op.drop_index("ix_jenkins_jobs_parent", table_name="jenkins_jobs")
    op.drop_table("jenkins_jobs")
