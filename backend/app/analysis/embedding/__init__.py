"""Embedding — Stage 3 埋め込みベクトル生成パッケージ。

extraction / classification と同型の Draft + Entity 2 層ドメインモデルを採用し、
Service は Outcome tagged union で Stage 1/2 と並ぶ実行結果型を返す。
"""

from app.analysis.embedding.domain import (
    EMBEDDING_DIMENSION,
    Embedding,
    EmbeddingVector,
)
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.embedding.service import (
    AlreadyEmbeddedOutcome,
    EmbeddedOutcome,
    EmbeddingOutcome,
    EmbeddingService,
    SkippedOutcome,
)

__all__ = [
    "EMBEDDING_DIMENSION",
    "AlreadyEmbeddedOutcome",
    "Embedding",
    "EmbeddedOutcome",
    "EmbeddingOutcome",
    "EmbeddingRepository",
    "EmbeddingService",
    "EmbeddingVector",
    "SkippedOutcome",
]
