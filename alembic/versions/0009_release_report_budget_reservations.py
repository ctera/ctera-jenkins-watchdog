"""Reserve Jenkins report budget only when work starts.

Revision ID: 0009_report_budget
Revises: 0008_jenkins_failure_reports
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_report_budget"
down_revision: str | None = "0008_jenkins_failure_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing report rows were queued with a full expected-token reservation.
    # Release that paper reservation and let the worker reserve only the row it claims.
    op.execute(
        """
        UPDATE investigation_requests
        SET reserved_tokens = 0, next_attempt_at = now(), error_summary = NULL
        WHERE source = 'jenkins_report' AND status = 'queued'
        """
    )


def downgrade() -> None:
    pass
