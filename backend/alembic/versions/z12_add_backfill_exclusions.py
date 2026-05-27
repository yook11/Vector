"""add stage 4/5 backfill exclusions.

Revision ID: z12_add_backfill_exclusions
Revises: z11_drop_pe_duration_ms
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "z12_add_backfill_exclusions"
down_revision: str | None = "z11_drop_pe_duration_ms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assessment_backfill_exclusions",
        sa.Column("curation_id", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=60), nullable=False),
        sa.Column(
            "excluded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason_code IN ('backfill_assessment_aged_out')",
            name="ck_assessment_backfill_exclusions_reason_code",
        ),
        sa.ForeignKeyConstraint(
            ["curation_id"],
            ["article_curations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("curation_id"),
    )
    op.create_table(
        "embedding_backfill_exclusions",
        sa.Column("analysis_id", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=60), nullable=False),
        sa.Column(
            "excluded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason_code IN ('backfill_embedding_aged_out')",
            name="ck_embedding_backfill_exclusions_reason_code",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["in_scope_assessments.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("analysis_id"),
    )


def downgrade() -> None:
    op.drop_table("embedding_backfill_exclusions")
    op.drop_table("assessment_backfill_exclusions")
