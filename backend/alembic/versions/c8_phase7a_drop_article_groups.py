"""Phase 7a: Drop article_groups table and article_group_id column.

Revision ID: c8a1b2c3d4e5
Revises: c7a1b2c3d4e5
Create Date: 2026-03-26
"""

import sqlalchemy as sa
from alembic import op

revision = "c8a1b2c3d4e5"
down_revision = "c7a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop FK constraints (use_alter requires explicit drop)
    op.drop_constraint(
        "news_articles_article_group_id_fkey",
        "news_articles",
        type_="foreignkey",
    )
    op.drop_constraint(
        "article_groups_canonical_id_fkey",
        "article_groups",
        type_="foreignkey",
    )

    # 2. Drop article_group_id column (index is dropped implicitly)
    op.drop_column("news_articles", "article_group_id")

    # 3. Drop article_groups table (idx_article_groups_canonical dropped implicitly)
    op.drop_table("article_groups")


def downgrade() -> None:
    # 1. Recreate article_groups table
    op.create_table(
        "article_groups",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("canonical_id", sa.Integer, nullable=True),
        sa.Column("article_count", sa.Integer, nullable=False, server_default="1"),
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

    # 2. Recreate article_group_id column on news_articles
    op.add_column(
        "news_articles",
        sa.Column("article_group_id", sa.Integer, nullable=True),
    )
    op.create_index(
        "ix_news_articles_article_group_id",
        "news_articles",
        ["article_group_id"],
        postgresql_where=sa.text("article_group_id IS NOT NULL"),
    )

    # 3. Restore FK constraints
    op.create_foreign_key(
        "news_articles_article_group_id_fkey",
        "news_articles",
        "article_groups",
        ["article_group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "article_groups_canonical_id_fkey",
        "article_groups",
        "news_articles",
        ["canonical_id"],
        ["id"],
        ondelete="SET NULL",
    )
