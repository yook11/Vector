"""Drop legacy analyses and analysis_translations tables.

These were replaced by article_analyses in Phase 4.

Revision ID: c17a1b2c3d4e
Revises: c16a1b2c3d4e
Create Date: 2026-03-29 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c17a1b2c3d4e"
down_revision: Union[str, None] = "c16a1b2c3d4e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("idx_analyses_impact", table_name="analyses")
    op.drop_index("idx_analyses_sentiment", table_name="analyses")
    op.drop_table("analysis_translations")
    op.drop_table("analyses")


def downgrade() -> None:
    op.create_table(
        "analyses",
        sa.Column("id", sa.INTEGER(), autoincrement=True, nullable=False),
        sa.Column(
            "news_article_id", sa.INTEGER(), autoincrement=False, nullable=False
        ),
        sa.Column(
            "sentiment", sa.VARCHAR(length=20), autoincrement=False, nullable=False
        ),
        sa.Column(
            "impact_score", sa.INTEGER(), autoincrement=False, nullable=False
        ),
        sa.Column("reasoning", sa.VARCHAR(), autoincrement=False, nullable=True),
        sa.Column(
            "analyzed_at",
            postgresql.TIMESTAMP(timezone=True),
            autoincrement=False,
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["news_article_id"],
            ["news_articles.id"],
            name="analyses_news_article_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="analyses_pkey"),
    )
    op.create_index("idx_analyses_sentiment", "analyses", ["sentiment"], unique=False)
    op.create_index("idx_analyses_impact", "analyses", ["impact_score"], unique=False)

    op.create_table(
        "analysis_translations",
        sa.Column("id", sa.INTEGER(), autoincrement=True, nullable=False),
        sa.Column(
            "analysis_id", sa.INTEGER(), autoincrement=False, nullable=False
        ),
        sa.Column(
            "locale", sa.VARCHAR(length=10), autoincrement=False, nullable=False
        ),
        sa.Column(
            "title", sa.VARCHAR(length=500), autoincrement=False, nullable=False
        ),
        sa.Column("summary", sa.TEXT(), autoincrement=False, nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analyses.id"],
            name="analysis_translations_analysis_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="analysis_translations_pkey"),
        sa.UniqueConstraint("analysis_id", "locale", name="uq_analysis_locale"),
    )
