"""Phase 4 Step 1: Add new columns to news_articles + create article_analyses table.

- Pre-migration cleanup: delete analyses rows where reasoning IS NULL (1 row)
- Add NULLABLE columns to news_articles: original_title, original_url,
  original_content, news_source_id, created_at
  (constraints tightened in Step 3 after data migration)
- Create article_analyses table with full constraints and HNSW index

Revision ID: c4a1b2c3d4e5
Revises: c3a1b2c3d4e5
Create Date: 2026-03-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4a1b2c3d4e5"
down_revision: str | None = "c3a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- 0. Pre-migration data cleanup ---
    # Delete analyses with reasoning IS NULL (1 row identified in Step 0).
    # Must run before article_analyses creation because Step 2 will migrate
    # all analyses rows, and reasoning NOT NULL would reject these.
    op.execute(
        "DELETE FROM analysis_translations "
        "WHERE analysis_id IN (SELECT id FROM analyses WHERE reasoning IS NULL)"
    )
    op.execute("DELETE FROM analyses WHERE reasoning IS NULL")

    # --- 1-A. Add new NULLABLE columns to news_articles ---
    # All columns are NULLABLE at this stage. After data migration (Step 2),
    # constraints will be tightened in Step 3.
    op.add_column(
        "news_articles",
        sa.Column("original_title", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "news_articles",
        sa.Column("original_url", sa.String(length=2048), nullable=True),
    )
    op.add_column(
        "news_articles",
        sa.Column("original_content", sa.Text(), nullable=True),
    )
    op.add_column(
        "news_articles",
        sa.Column("news_source_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "news_articles",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- 1-B. Create article_analyses table with full constraints ---
    op.create_table(
        "article_analyses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("news_article_id", sa.Integer(), nullable=False),
        sa.Column("translated_title", sa.String(length=500), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("impact_level", sa.String(length=20), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("ai_model", sa.String(length=100), nullable=False),
        sa.Column(
            "analyzed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["news_article_id"],
            ["news_articles.id"],
            name="fk_article_analyses_news_article_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "news_article_id",
            name="uq_article_analyses_news_article_id",
        ),
        sa.CheckConstraint(
            "impact_level IN ('low', 'medium', 'high', 'critical')",
            name="ck_article_analyses_impact_level",
        ),
    )

    # HNSW index for cosine similarity search on embeddings
    op.execute(
        "CREATE INDEX idx_article_analyses_embedding "
        "ON article_analyses "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    # --- Drop article_analyses table and index ---
    op.drop_index("idx_article_analyses_embedding", table_name="article_analyses")
    op.drop_table("article_analyses")

    # --- Drop new columns from news_articles ---
    op.drop_column("news_articles", "created_at")
    op.drop_column("news_articles", "news_source_id")
    op.drop_column("news_articles", "original_content")
    op.drop_column("news_articles", "original_url")
    op.drop_column("news_articles", "original_title")

    # NOTE: The pre-migration cleanup (deletion of analyses rows with
    # reasoning IS NULL) cannot be reversed. Those rows are permanently lost
    # after upgrade.
