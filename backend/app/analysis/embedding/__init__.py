"""Embedding — Stage 5 埋め込みベクトル生成パッケージ。

Stage 5 は pipeline 終端のため、Service は副作用のみ (永続化) を担い
戻り値 ``None`` 一本化で運用する。読み戻し / Outcome dispatch / Entity 復元は
廃止済み (2026-05-12)。楽観ロックで並行 update に先を越された場合は log +
短絡で抜ける (Stage 4 Assessment と同型)。
"""

from app.analysis.embedding.domain import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.embedding.service import EmbeddingService

__all__ = [
    "EMBEDDING_DIMENSION",
    "EmbeddingRepository",
    "EmbeddingService",
    "EmbeddingVector",
]
