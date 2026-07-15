"""Persist analysis selection, LLM costs, and budget reservations.

Revision ID: 0005_analysis_costs
Revises: 0004_agent_investigations
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_analysis_costs"
down_revision: str | None = "0004_agent_investigations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.add_column(
        "investigation_requests",
        sa.Column("budget_kind", sa.String(length=16), server_default="automatic", nullable=False),
    )
    op.add_column(
        "investigation_requests",
        sa.Column("reserved_tokens", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "jenkins_builds",
        sa.Column("recovered", sa.Boolean(), server_default=sa.false(), nullable=False),
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_read_input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_creation_input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("cost_source", sa.String(length=32), server_default="unavailable", nullable=False),
        sa.Column("budget_kind", sa.String(length=16), nullable=True),
        sa.Column("incident_id", sa.Uuid(), nullable=True),
        sa.Column("investigation_id", sa.Uuid(), nullable=True),
        sa.Column("scan_id", sa.Uuid(), nullable=True),
        sa.Column("metadata", JSON, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["investigation_id"], ["investigations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["scan_id"], ["scans.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_calls_created", "llm_calls", ["created_at"])
    op.create_index("ix_llm_calls_incident_created", "llm_calls", ["incident_id", "created_at"])
    op.create_index("ix_llm_calls_investigation", "llm_calls", ["investigation_id", "created_at"])
    op.create_index("ix_llm_calls_scan", "llm_calls", ["scan_id", "created_at"])

    op.create_table(
        "analysis_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=False),
        sa.Column("occurrence_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("scan_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        sa.Column("llm_call_id", sa.Uuid(), nullable=True),
        sa.Column("metadata", JSON, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["occurrence_id"], ["incident_occurrences.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scan_id"], ["scans.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["request_id"], ["investigation_requests.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["llm_call_id"], ["llm_calls.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_analysis_decisions_incident_created",
        "analysis_decisions",
        ["incident_id", "created_at"],
    )
    op.create_index(
        "ix_analysis_decisions_outcome_created",
        "analysis_decisions",
        ["outcome", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_analysis_decisions_outcome_created", table_name="analysis_decisions")
    op.drop_index("ix_analysis_decisions_incident_created", table_name="analysis_decisions")
    op.drop_table("analysis_decisions")
    op.drop_index("ix_llm_calls_scan", table_name="llm_calls")
    op.drop_index("ix_llm_calls_investigation", table_name="llm_calls")
    op.drop_index("ix_llm_calls_incident_created", table_name="llm_calls")
    op.drop_index("ix_llm_calls_created", table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_column("jenkins_builds", "recovered")
    op.drop_column("investigation_requests", "reserved_tokens")
    op.drop_column("investigation_requests", "budget_kind")
