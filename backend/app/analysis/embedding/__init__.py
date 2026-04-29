"""Embedding — Stage E 埋め込みベクトル生成パッケージ。

extraction / classification と同型の Draft + Entity 2 層ドメインモデルを採用し、
Service は Outcome tagged union で並ぶ実行結果型を返す。Pattern A'
(typed-pipeline-preconditions.md §1.1) では precondition は ``ReadyForEmbedding``
が構造保証するため Outcome は ``EmbeddedOutcome | InvalidInputOutcome`` の 2
variants に縮退する。
"""

from app.analysis.embedding.domain import (
    EMBEDDING_DIMENSION,
    Embedding,
    EmbeddingVector,
)
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.embedding.service import (
    EmbeddedOutcome,
    EmbeddingOutcome,
    EmbeddingService,
    InvalidInputOutcome,
)

__all__ = [
    "EMBEDDING_DIMENSION",
    "Embedding",
    "EmbeddedOutcome",
    "EmbeddingOutcome",
    "EmbeddingRepository",
    "EmbeddingService",
    "EmbeddingVector",
    "InvalidInputOutcome",
]
