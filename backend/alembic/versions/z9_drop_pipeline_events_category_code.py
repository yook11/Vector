"""drop legacy category and code from pipeline_events.

Revision ID: z9_drop_pe_category_code
Revises: z8_pipeline_events_retryability
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "z9_drop_pe_category_code"
down_revision: str | None = "z8_pipeline_events_retryability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CATEGORY_VALUES = (
    "success",
    "idempotent_skip",
    "retryable",
    "non_retryable_drop_article",
    "non_retryable_keep_article",
    "non_retryable_keep_curation",
    "non_retryable",
    "unknown",
)


def upgrade() -> None:
    op.drop_constraint("ck_pipeline_events_category", "pipeline_events", type_="check")
    op.drop_column("pipeline_events", "code")
    op.drop_column("pipeline_events", "category")


def downgrade() -> None:
    op.add_column(
        "pipeline_events",
        sa.Column("category", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "pipeline_events",
        sa.Column("code", sa.String(length=60), nullable=True),
    )
    values_sql = ",".join(f"'{value}'" for value in _CATEGORY_VALUES)
    op.create_check_constraint(
        "ck_pipeline_events_category",
        "pipeline_events",
        f"category IS NULL OR category IN ({values_sql})",
    )
