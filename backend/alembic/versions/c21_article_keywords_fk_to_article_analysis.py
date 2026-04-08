"""Migrate article_keywords FK from news_articles to article_analyses.

Step 1: Add article_analysis_id column (nullable) with FK.
Step 2: Data migration — populate from article_analyses, delete orphans.
Step 3: Tighten — NOT NULL on new column, drop old FK/PK/column, new PK.

Revision ID: c21a1b2c3d4e
Revises: c20a1b2c3d4e
Create Date: 2026-04-09 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c21a1b2c3d4e"
down_revision: Union[str, None] = "c20a1b2c3d4e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: Add nullable column + FK
    op.add_column(
        "article_keywords",
        sa.Column("article_analysis_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_article_keywords_article_analysis_id",
        "article_keywords",
        "article_analyses",
        ["article_analysis_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Step 2: Data migration
    op.execute(
        """
        UPDATE article_keywords ak
        SET article_analysis_id = aa.id
        FROM article_analyses aa
        WHERE aa.news_article_id = ak.news_article_id
        """
    )
    op.execute(
        "DELETE FROM article_keywords WHERE article_analysis_id IS NULL"
    )

    # Step 3a: Finalize new column
    op.alter_column(
        "article_keywords", "article_analysis_id", nullable=False
    )

    # Step 3b: Drop old FK first, then PK (required before column drop)
    op.drop_constraint(
        "news_keywords_news_article_id_fkey", "article_keywords", type_="foreignkey"
    )
    op.drop_constraint("pk_article_keywords", "article_keywords", type_="primary")
    op.drop_column("article_keywords", "news_article_id")

    # Step 3c: New PK
    op.create_primary_key(
        "pk_article_keywords",
        "article_keywords",
        ["article_analysis_id", "keyword_id"],
    )


def downgrade() -> None:
    # Remove new PK
    op.drop_constraint("pk_article_keywords", "article_keywords", type_="primary")

    # Re-add old column
    op.add_column(
        "article_keywords",
        sa.Column("news_article_id", sa.Integer(), nullable=True),
    )
    op.execute(
        """
        UPDATE article_keywords ak
        SET news_article_id = aa.news_article_id
        FROM article_analyses aa
        WHERE aa.id = ak.article_analysis_id
        """
    )
    op.execute(
        "DELETE FROM article_keywords WHERE news_article_id IS NULL"
    )
    op.alter_column(
        "article_keywords", "news_article_id", nullable=False
    )

    # Remove new column
    op.drop_constraint(
        "fk_article_keywords_article_analysis_id",
        "article_keywords",
        type_="foreignkey",
    )
    op.drop_column("article_keywords", "article_analysis_id")

    # Restore old PK + FK
    op.create_primary_key(
        "pk_article_keywords",
        "article_keywords",
        ["news_article_id", "keyword_id"],
    )
    op.create_foreign_key(
        "news_keywords_news_article_id_fkey",
        "article_keywords",
        "news_articles",
        ["news_article_id"],
        ["id"],
        ondelete="CASCADE",
    )
