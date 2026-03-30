"""add article_analyses check constraints

Revision ID: 6b3f8ec9a583
Revises: 7b90b86f5207
Create Date: 2026-03-30 10:37:16.694071

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6b3f8ec9a583"
down_revision: str | None = "7b90b86f5207"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_article_analyses_translated_title_not_empty",
        "article_analyses",
        "translated_title != ''",
    )
    op.create_check_constraint(
        "ck_article_analyses_summary_not_empty",
        "article_analyses",
        "summary != ''",
    )
    op.create_check_constraint(
        "ck_article_analyses_reasoning_not_empty",
        "article_analyses",
        "reasoning != ''",
    )
    op.create_check_constraint(
        "ck_article_analyses_ai_model_not_empty",
        "article_analyses",
        "ai_model != ''",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_article_analyses_ai_model_not_empty",
        "article_analyses",
    )
    op.drop_constraint(
        "ck_article_analyses_reasoning_not_empty",
        "article_analyses",
    )
    op.drop_constraint(
        "ck_article_analyses_summary_not_empty",
        "article_analyses",
    )
    op.drop_constraint(
        "ck_article_analyses_translated_title_not_empty",
        "article_analyses",
    )
