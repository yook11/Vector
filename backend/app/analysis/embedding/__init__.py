"""Embedding — Stage 5 埋め込みベクトル生成パッケージ。

Stage 5 は pipeline 終端のため、Service は副作用のみ (永続化) を担い、
楽観ロックで並行 update に先を越された場合は log + 短絡で抜ける。

エラー taxonomy (Stage 4 Assessment と完全同形、``errors.py`` 参照):
- Layer 1 marker (``EmbeddingRecoverableError`` / ``EmbeddingTerminalError``):
  Task 層 marker dispatch + catch-all の軸
- Layer 2-B (``EmbeddingResponseInvalidError``): ``EmbeddingVector`` VO 構造違反
  を Recoverable で wrap
- Layer 2-A ACL (``to_embedding_error``): provider 由来 ``AIProviderError`` を
  Stage 5 marker に詰め替える Service 境界
"""

from app.analysis.embedding.domain import (
    EMBEDDING_DIMENSION,
    EmbeddingVector,
)
from app.analysis.embedding.errors import (
    EmbeddingError,
    EmbeddingRecoverableError,
    EmbeddingResponseInvalidError,
    EmbeddingTerminalError,
    EmbeddingTerminalStageBlockedError,
    EmbeddingTerminalTargetRejectedError,
)
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.embedding.service import EmbeddingService
from app.audit.stages.embedding import EmbeddingAuditRepository

__all__ = [
    "EMBEDDING_DIMENSION",
    "EmbeddingAuditRepository",
    "EmbeddingError",
    "EmbeddingRecoverableError",
    "EmbeddingRepository",
    "EmbeddingResponseInvalidError",
    "EmbeddingService",
    "EmbeddingTerminalError",
    "EmbeddingTerminalStageBlockedError",
    "EmbeddingTerminalTargetRejectedError",
    "EmbeddingVector",
]
