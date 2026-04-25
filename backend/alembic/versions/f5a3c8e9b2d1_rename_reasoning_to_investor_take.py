"""rename_reasoning_to_investor_take

Rename `reasoning` column to `investor_take` on both `article_analyses` and
`article_rejections` tables. The semantic shift is from "classification
rationale" to "investor's perspective reading" — reflecting the broader move
from semantic search to analysis-driven reporting. Existing data stays as-is
(legacy meaning); new rows from the classifier will carry the new meaning.

Revision ID: f5a3c8e9b2d1
Revises: 9304ea71c183
Create Date: 2026-04-25 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "f5a3c8e9b2d1"
down_revision: str | None = "9304ea71c183"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")

    # article_analyses
    op.drop_constraint(
        "ck_article_analyses_reasoning_not_empty",
        "article_analyses",
        type_="check",
    )
    op.alter_column(
        "article_analyses",
        "reasoning",
        new_column_name="investor_take",
    )
    op.create_check_constraint(
        "ck_article_analyses_investor_take_not_empty",
        "article_analyses",
        "investor_take != ''",
    )

    # article_rejections
    op.drop_constraint(
        "ck_article_rejections_reasoning_not_empty",
        "article_rejections",
        type_="check",
    )
    op.alter_column(
        "article_rejections",
        "reasoning",
        new_column_name="investor_take",
    )
    op.create_check_constraint(
        "ck_article_rejections_investor_take_not_empty",
        "article_rejections",
        "investor_take != ''",
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")

    # article_rejections
    op.drop_constraint(
        "ck_article_rejections_investor_take_not_empty",
        "article_rejections",
        type_="check",
    )
    op.alter_column(
        "article_rejections",
        "investor_take",
        new_column_name="reasoning",
    )
    op.create_check_constraint(
        "ck_article_rejections_reasoning_not_empty",
        "article_rejections",
        "reasoning != ''",
    )

    # article_analyses
    op.drop_constraint(
        "ck_article_analyses_investor_take_not_empty",
        "article_analyses",
        type_="check",
    )
    op.alter_column(
        "article_analyses",
        "investor_take",
        new_column_name="reasoning",
    )
    op.create_check_constraint(
        "ck_article_analyses_reasoning_not_empty",
        "article_analyses",
        "reasoning != ''",
    )
