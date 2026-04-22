"""create article_rejections for OutOfScope classifications.

Stage 2 で対象外（先端テックに該当しない、または分類不能）と判定された extraction の記録用。
article_analyses とは排他関係（同一 extraction に対してどちらか一方のみが存在可能）。

Revision ID: d2e3f4a5b6c7
Revises: d6f7a8b9c0d1
Create Date: 2026-04-22
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2e3f4a5b6c7"
down_revision: str | None = "d6f7a8b9c0d1"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.create_table(
        "article_rejections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "extraction_id",
            sa.Integer(),
            sa.ForeignKey("article_extractions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("ai_model", sa.String(100), nullable=False),
        sa.Column(
            "rejected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "extraction_id", name="uq_article_rejections_extraction_id"
        ),
        sa.CheckConstraint(
            "reasoning != ''",
            name="ck_article_rejections_reasoning_not_empty",
        ),
        sa.CheckConstraint(
            "ai_model != ''",
            name="ck_article_rejections_ai_model_not_empty",
        ),
    )


def downgrade() -> None:
    op.drop_table("article_rejections")
