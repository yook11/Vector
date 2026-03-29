"""Drop unnecessary indexes from news_articles, fetch_logs, article_analyses.

1. idx_news_created — redundant with idx_news_published
2. ix_fetch_logs_source_id_fetched_at — single source_id index suffices
3. idx_article_analyses_embedding (HNSW) — will be re-added in 3B-2

Revision ID: c18a1b2c3d4e
Revises: c17a1b2c3d4e
Create Date: 2026-03-29 13:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c18a1b2c3d4e"
down_revision: Union[str, None] = "c17a1b2c3d4e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("idx_news_created", table_name="news_articles")
    op.drop_index(
        "ix_fetch_logs_source_id_fetched_at", table_name="fetch_logs"
    )
    op.drop_index(
        "idx_article_analyses_embedding", table_name="article_analyses"
    )


def downgrade() -> None:
    op.create_index(
        "idx_news_created",
        "news_articles",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_fetch_logs_source_id_fetched_at",
        "fetch_logs",
        ["source_id", "fetched_at"],
        unique=False,
    )
    op.execute(
        "CREATE INDEX idx_article_analyses_embedding "
        "ON article_analyses USING hnsw (embedding vector_cosine_ops)"
    )
