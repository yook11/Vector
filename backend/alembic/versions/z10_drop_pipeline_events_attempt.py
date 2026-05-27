"""drop attempt from pipeline_events.

Revision ID: z10_drop_pipeline_events_attempt
Revises: z9_drop_pe_category_code
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "z10_drop_pipeline_events_attempt"
down_revision: str | None = "z9_drop_pe_category_code"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_pipeline_events_attempt_positive", "pipeline_events", type_="check"
    )
    op.drop_column("pipeline_events", "attempt")


def downgrade() -> None:
    op.add_column(
        "pipeline_events",
        sa.Column(
            "attempt",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_check_constraint(
        "ck_pipeline_events_attempt_positive",
        "pipeline_events",
        "attempt >= 1",
    )
