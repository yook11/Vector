"""add retryability to pipeline_events.

Revision ID: z8_pipeline_events_retryability
Revises: z7_drop_fetch_logs
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "z8_pipeline_events_retryability"
down_revision: str | None = "z7_drop_fetch_logs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RETRYABILITY_VALUES = ("retryable", "non_retryable", "unknown")


def upgrade() -> None:
    op.add_column(
        "pipeline_events",
        sa.Column("retryability", sa.String(length=20), nullable=True),
    )
    values_sql = ",".join(f"'{value}'" for value in _RETRYABILITY_VALUES)
    op.create_check_constraint(
        "ck_pipeline_events_retryability",
        "pipeline_events",
        f"retryability IS NULL OR retryability IN ({values_sql})",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_pipeline_events_retryability", "pipeline_events", type_="check"
    )
    op.drop_column("pipeline_events", "retryability")
