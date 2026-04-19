"""embedding カラムを Vector(768) から HALFVEC(768) に変更。

半精度浮動小数点型への移行でストレージを約 50% 削減する。
HNSW インデックスの ops class も halfvec_cosine_ops に変更。

Revision ID: g4d5e6f7h8i9
Revises: 1ee30c910254
Create Date: 2026-04-19
"""

from alembic import op

revision = "g4d5e6f7h8i9"
down_revision = "1ee30c910254"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_article_analyses_embedding")
    op.execute(
        "ALTER TABLE article_analyses "
        "ALTER COLUMN embedding TYPE halfvec(768) "
        "USING embedding::halfvec(768)"
    )
    op.execute(
        "CREATE INDEX idx_article_analyses_embedding "
        "ON article_analyses USING hnsw (embedding halfvec_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_article_analyses_embedding")
    op.execute(
        "ALTER TABLE article_analyses "
        "ALTER COLUMN embedding TYPE vector(768) "
        "USING embedding::vector(768)"
    )
    op.execute(
        "CREATE INDEX idx_article_analyses_embedding "
        "ON article_analyses USING hnsw (embedding vector_cosine_ops)"
    )
