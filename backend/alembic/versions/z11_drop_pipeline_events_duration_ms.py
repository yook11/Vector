"""drop duration_ms from pipeline_events.

Revision ID: z11_drop_pe_duration_ms
Revises: z10_drop_pipeline_events_attempt
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "z11_drop_pe_duration_ms"
down_revision: str | None = "z10_drop_pipeline_events_attempt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_pipeline_events_duration_nonneg", "pipeline_events", type_="check"
    )
    op.drop_column("pipeline_events", "duration_ms")


def downgrade() -> None:
    op.add_column(
        "pipeline_events",
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_pipeline_events_duration_nonneg",
        "pipeline_events",
        "duration_ms IS NULL OR duration_ms >= 0",
    )
