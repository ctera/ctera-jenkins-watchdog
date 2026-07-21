"""Link internal Jenkins failure reports to scans.

Revision ID: 0011_report_scan
Revises: 0010_assessment_fields
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_report_scan"
down_revision: str | None = "0010_assessment_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jenkins_failure_reports", sa.Column("scan_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_jenkins_failure_reports_scan_id",
        "jenkins_failure_reports",
        "scans",
        ["scan_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "uq_jenkins_failure_reports_scan",
        "jenkins_failure_reports",
        ["scan_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_jenkins_failure_reports_scan", table_name="jenkins_failure_reports")
    op.drop_constraint("fk_jenkins_failure_reports_scan_id", "jenkins_failure_reports", type_="foreignkey")
    op.drop_column("jenkins_failure_reports", "scan_id")
