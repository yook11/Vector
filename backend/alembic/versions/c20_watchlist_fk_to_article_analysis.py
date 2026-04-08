"""Migrate watchlist_entries FK from news_articles to article_analyses.

Step 1: Add article_analysis_id column (nullable) with FK.
Step 2: Data migration — populate from article_analyses, delete orphans.
Step 3: Tighten — NOT NULL on new column, drop old column, replace PK.

Revision ID: c20a1b2c3d4e
Revises: c19a1b2c3d4e
Create Date: 2026-04-08 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c20a1b2c3d4e"
down_revision: Union[str, None] = "f69872f78e9f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: Add nullable column + FK
    op.add_column(
        "watchlist_entries",
        sa.Column("article_analysis_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_watchlist_entries_article_analysis_id",
        "watchlist_entries",
        "article_analyses",
        ["article_analysis_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Step 2: Data migration
    op.execute(
        """
        UPDATE watchlist_entries we
        SET article_analysis_id = aa.id
        FROM article_analyses aa
        WHERE aa.news_article_id = we.news_article_id
        """
    )
    op.execute(
        "DELETE FROM watchlist_entries WHERE article_analysis_id IS NULL"
    )

    # Step 3a: Finalize new column
    op.alter_column(
        "watchlist_entries", "article_analysis_id", nullable=False
    )

    # Step 3b: Remove old column
    op.drop_constraint("watchlist_entries_pkey", "watchlist_entries", type_="primary")
    op.drop_constraint(
        "fk_watchlist_entries_news_article_id", "watchlist_entries", type_="foreignkey"
    )
    op.drop_column("watchlist_entries", "news_article_id")

    # Step 3c: New PK
    op.create_primary_key(
        "watchlist_entries_pkey",
        "watchlist_entries",
        ["user_id", "article_analysis_id"],
    )


def downgrade() -> None:
    # Remove new PK
    op.drop_constraint("watchlist_entries_pkey", "watchlist_entries", type_="primary")

    # Re-add old column
    op.add_column(
        "watchlist_entries",
        sa.Column("news_article_id", sa.Integer(), nullable=True),
    )
    op.execute(
        """
        UPDATE watchlist_entries we
        SET news_article_id = aa.news_article_id
        FROM article_analyses aa
        WHERE aa.id = we.article_analysis_id
        """
    )
    op.execute(
        "DELETE FROM watchlist_entries WHERE news_article_id IS NULL"
    )
    op.alter_column(
        "watchlist_entries", "news_article_id", nullable=False
    )

    # Remove new column
    op.drop_constraint(
        "fk_watchlist_entries_article_analysis_id", "watchlist_entries", type_="foreignkey"
    )
    op.drop_column("watchlist_entries", "article_analysis_id")

    # Restore old PK + FK
    op.create_primary_key(
        "watchlist_entries_pkey",
        "watchlist_entries",
        ["user_id", "news_article_id"],
    )
    op.create_foreign_key(
        "fk_watchlist_entries_news_article_id",
        "watchlist_entries",
        "news_articles",
        ["news_article_id"],
        ["id"],
        ondelete="CASCADE",
    )
