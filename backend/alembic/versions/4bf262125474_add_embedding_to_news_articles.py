"""add embedding to news_articles

Revision ID: 4bf262125474
Revises: 3a9bf03a0b5f
Create Date: 2026-02-21 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = '4bf262125474'
down_revision: Union[str, None] = '3a9bf03a0b5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: Enable pgvector extension (idempotent — safe to run multiple times).
    # Must be executed before adding the Vector column.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Step 2: Add the embedding column (nullable so existing rows remain valid).
    op.add_column(
        "news_articles",
        sa.Column("embedding", Vector(768), nullable=True),
    )

    # Step 3: Create HNSW index for cosine distance similarity search.
    # Note: CONCURRENTLY cannot be used inside a transaction block (Alembic default).
    op.execute(
        "CREATE INDEX idx_news_embedding "
        "ON news_articles "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_index("idx_news_embedding", table_name="news_articles")
    op.drop_column("news_articles", "embedding")
    # Do NOT drop the vector extension — other tables may depend on it.
