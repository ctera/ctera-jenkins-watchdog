"""Durable tool-backed investigation queue and Jenkins incident links.

Revision ID: 0004_agent_investigations
Revises: 0003_jenkins_observability
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_agent_investigations"
down_revision: str | None = "0003_jenkins_observability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jenkins_builds", sa.Column("incident_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_jenkins_builds_incident_id",
        "jenkins_builds",
        "incidents",
        ["incident_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_jenkins_builds_incident", "jenkins_builds", ["incident_id", "started_at"])

    op.create_table(
        "investigation_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("occurrence_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("scan_id", sa.Uuid(), nullable=True),
        sa.Column("build_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by", sa.String(length=320), nullable=True),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("investigation_id", sa.Uuid(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["build_id"], ["jenkins_builds.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["investigation_id"], ["investigations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["occurrence_id"], ["incident_occurrences.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scan_id"], ["scans.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_investigation_requests_active_incident",
        "investigation_requests",
        ["incident_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_investigation_requests_claim",
        "investigation_requests",
        ["status", "next_attempt_at", "lease_expires_at", "priority"],
    )
    op.create_index(
        "ix_investigation_requests_incident_created",
        "investigation_requests",
        ["incident_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_investigation_requests_incident_created", table_name="investigation_requests")
    op.drop_index("ix_investigation_requests_claim", table_name="investigation_requests")
    op.drop_index("uq_investigation_requests_active_incident", table_name="investigation_requests")
    op.drop_table("investigation_requests")
    op.drop_index("ix_jenkins_builds_incident", table_name="jenkins_builds")
    op.drop_constraint("fk_jenkins_builds_incident_id", "jenkins_builds", type_="foreignkey")
    op.drop_column("jenkins_builds", "incident_id")
