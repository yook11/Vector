"""Phase 7c-1: Drop write-only legacy columns from news_articles.

Removes 6 columns that were kept only for parallel writes during
the Phase 4 migration: title_original, url, source, source_id,
fetched_at, embedding.  Also drops idx_news_fetched.

Revision ID: c10a1b2c3d4e
Revises: c9a1b2c3d4e5
Create Date: 2026-03-26
"""

import sqlalchemy as sa
from alembic import op

revision = "c10a1b2c3d4e"
down_revision = "c9a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop FK on legacy source_id
    op.execute(
        "ALTER TABLE news_articles "
        "DROP CONSTRAINT IF EXISTS news_articles_source_id_fkey"
    )

    # 2. Drop legacy index
    op.execute("DROP INDEX IF EXISTS idx_news_fetched")

    # 3. Drop columns
    op.drop_column("news_articles", "title_original")
    op.drop_column("news_articles", "url")
    op.drop_column("news_articles", "source")
    op.drop_column("news_articles", "source_id")
    op.drop_column("news_articles", "fetched_at")
    op.drop_column("news_articles", "embedding")


def downgrade() -> None:
    # Re-add columns
    op.add_column(
        "news_articles",
        sa.Column("title_original", sa.VARCHAR(500), nullable=True),
    )
    op.add_column(
        "news_articles",
        sa.Column("url", sa.VARCHAR(2048), nullable=True),
    )
    op.add_column(
        "news_articles",
        sa.Column("source", sa.VARCHAR(100), nullable=True),
    )
    op.add_column(
        "news_articles",
        sa.Column("source_id", sa.INTEGER(), nullable=True),
    )
    op.add_column(
        "news_articles",
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
    )
    # pgvector must be available for this downgrade
    op.execute(
        "ALTER TABLE news_articles ADD COLUMN embedding vector(768)"
    )

    # Backfill from new columns
    op.execute(
        "UPDATE news_articles SET "
        "title_original = original_title, "
        "url = original_url, "
        "source = '', "
        "source_id = news_source_id, "
        "fetched_at = created_at"
    )

    # Restore NOT NULL constraints
    op.alter_column("news_articles", "title_original", nullable=False)
    op.alter_column("news_articles", "url", nullable=False)
    op.alter_column("news_articles", "source", nullable=False)
    op.alter_column("news_articles", "fetched_at", nullable=False)

    # Restore index and FK
    op.create_index("idx_news_fetched", "news_articles", ["fetched_at"])
    op.create_foreign_key(
        "news_articles_source_id_fkey",
        "news_articles",
        "news_sources",
        ["source_id"],
        ["id"],
        ondelete="SET NULL",
    )
