"""Backfill required structured assessment fields.

Revision ID: 0010_assessment_fields
Revises: 0009_report_budget
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_assessment_fields"
down_revision: str | None = "0009_report_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Earlier model responses could explicitly return null for these required fields.
    # Normalize persisted assessments so existing report rows meet the same contract as new ones.
    op.execute(
        """
        UPDATE investigations
        SET result = jsonb_set(
            jsonb_set(
                result,
                '{plain_language_summary}',
                CASE
                    WHEN NULLIF(result->>'plain_language_summary', '') IS NOT NULL
                        THEN result->'plain_language_summary'
                    ELSE to_jsonb(COALESCE(NULLIF(result->>'root_cause', ''), 'Root cause was not provided by the agent.'))
                END,
                true
            ),
            '{verification_steps}',
            CASE
                WHEN jsonb_typeof(result->'verification_steps') = 'array'
                    AND jsonb_array_length(result->'verification_steps') > 0
                    THEN result->'verification_steps'
                ELSE jsonb_build_array('Run the affected Jenkins build again and confirm the cited failure is absent.')
            END,
            true
        )
        WHERE result IS NOT NULL
          AND (
              NULLIF(result->>'plain_language_summary', '') IS NULL
              OR jsonb_typeof(result->'verification_steps') IS DISTINCT FROM 'array'
              OR (
                  jsonb_typeof(result->'verification_steps') = 'array'
                  AND jsonb_array_length(result->'verification_steps') = 0
              )
          )
        """
    )


def downgrade() -> None:
    pass
