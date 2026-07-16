"""Index scan-linked analysis progress.

Revision ID: 0007_scan_analysis_indexes
Revises: 0006_source_attribution
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_scan_analysis_indexes"
down_revision: str | None = "0006_source_attribution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_investigation_requests_scan_status",
        "investigation_requests",
        ["scan_id", "status", "created_at"],
    )
    op.create_index(
        "ix_analysis_decisions_scan_created",
        "analysis_decisions",
        ["scan_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_analysis_decisions_scan_created", table_name="analysis_decisions")
    op.drop_index("ix_investigation_requests_scan_status", table_name="investigation_requests")
