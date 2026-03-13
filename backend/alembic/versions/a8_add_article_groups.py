"""add article_groups table and news_articles.article_group_id

Revision ID: a8b9c0d1e2f3
Revises: a7b8c9d0e1f2
Create Date: 2026-03-08 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create article_groups table
    op.create_table(
        "article_groups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "canonical_id",
            sa.Integer(),
            sa.ForeignKey("news_articles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("article_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_article_groups_canonical",
        "article_groups",
        ["canonical_id"],
    )

    # 2. Add article_group_id FK to news_articles
    op.add_column(
        "news_articles",
        sa.Column(
            "article_group_id",
            sa.Integer(),
            sa.ForeignKey("article_groups.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_articles_group_id",
        "news_articles",
        ["article_group_id"],
        postgresql_where=sa.text("article_group_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_articles_group_id", table_name="news_articles")
    op.drop_column("news_articles", "article_group_id")
    op.drop_index("idx_article_groups_canonical", table_name="article_groups")
    op.drop_table("article_groups")
