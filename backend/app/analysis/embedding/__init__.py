"""Embedding — Stage 3 埋め込みベクトル生成パッケージ。

extraction / classification と同型の Draft + Entity 2 層ドメインモデルを採用する。
Repository / Service / Outcome は Phase 2 以降で追加する。
"""

from app.analysis.embedding.domain import (
    EMBEDDING_DIMENSION,
    Embedding,
    EmbeddingVector,
)

__all__ = ["EMBEDDING_DIMENSION", "Embedding", "EmbeddingVector"]
