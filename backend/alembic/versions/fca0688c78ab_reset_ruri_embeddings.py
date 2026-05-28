"""reset Ruri-v3 embeddings to NULL for re-generation by Gemini

embedding 生成 provider を TEI (Ruri-v3-310m) から Gemini (gemini-embedding-001)
に切り替えるにあたり、両者はベクトル空間が異なる (output_dimensionality は同じ
768 だが意味空間が違う) ため、既存の Ruri ベクトルは Gemini ベクトルと混在させ
られない。本マイグレーションでは ``embedding_model = 'cl-nagoya/ruri-v3-310m'``
の行を NULL 化し、既存 backfill_embeddings cron が ``embedding IS NULL`` を
拾って Gemini で再生成する経路に乗せる。

CHECK 制約 ``ck_article_analyses_embedding_consistency`` は ``embedding`` と
``embedding_model`` の両方 NULL or 両方 NOT NULL を要求するため、両方を同時に
NULL に更新する (片側だけだと制約違反)。

HNSW インデックス ``idx_article_analyses_embedding`` は DROP しない。pgvector
HNSW は incremental 追加に対応しており、backfill による段階的な再構築で十分。

Revision ID: fca0688c78ab
Revises: k7_articles_source_strict
Create Date: 2026-05-01 04:20:27.422498

"""

from collections.abc import Sequence

from alembic import op

revision: str = "fca0688c78ab"
down_revision: str | None = "k7_articles_source_strict"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute(
        """
        UPDATE article_analyses
        SET embedding = NULL,
            embedding_model = NULL
        WHERE embedding_model = 'cl-nagoya/ruri-v3-310m'
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Rolling back the Ruri embedding reset is not supported. "
        "Re-generate via backfill_embeddings cron after rollback."
    )
