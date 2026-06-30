"""内部検索 query 埋め込みのキャッシュ (ORM)。

planner が選んだ embed 対象テキストの sha256 と embedder 同一性で exact-match
ルックアップし、ヒットすれば保存済みベクトルを再利用して再 embed を省く。平文
query は保存しない (hash と vector だけ持ち PII-at-rest を避ける)。corpus と同じ
空間で扱うため HALFVEC を corpus 側と同次元で揃える。
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import CheckConstraint, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["QueryEmbeddingCache"]


class QueryEmbeddingCache(Base):
    """embed 対象テキストの hash と embedder 同一性で引く query 埋め込みキャッシュ。"""

    __tablename__ = "query_embedding_cache"
    __table_args__ = (
        # exact-match ルックアップのキー兼 backing index。embedder_identity を含めて
        # model/task_type/次元が変わった行を別空間化し stale hit を防ぐ。近傍検索は
        # しないため HNSW index は張らない。
        UniqueConstraint(
            "query_hash",
            "embedder_identity",
            name="uq_query_embedding_cache_hash_identity",
        ),
        # query_hash が sha256 hex (64 字) であることを構造的に保証する。
        CheckConstraint(
            "char_length(query_hash) = 64",
            name="ck_query_embedding_cache_query_hash_len",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    query_hash: Mapped[str] = mapped_column(String(64))
    embedder_identity: Mapped[str] = mapped_column(String(255))
    # corpus 側 (analyzed_articles.embedding) と同次元。model 層を analysis BC に
    # 依存させないため SSoT 定数を import せずリテラルで揃える (既存 model と同規約)。
    query_vector: Mapped[list[float]] = mapped_column(HALFVEC(768))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
