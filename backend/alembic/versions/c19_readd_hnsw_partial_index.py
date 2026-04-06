"""Re-add HNSW index on article_analyses.embedding as a partial index.

Dropped in c18; re-added here with WHERE embedding IS NOT NULL
so NULL rows are excluded from the index.

Revision ID: c19a1b2c3d4e
Revises: c18a1b2c3d4e
Create Date: 2026-03-29 14:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c19a1b2c3d4e"
down_revision: Union[str, None] = "c18a1b2c3d4e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX idx_article_analyses_embedding "
        "ON article_analyses USING hnsw (embedding vector_cosine_ops) "
        "WHERE embedding IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("idx_article_analyses_embedding", table_name="article_analyses")
