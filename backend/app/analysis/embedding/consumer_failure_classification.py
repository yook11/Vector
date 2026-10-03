"""Consumerの失敗から、再配信に任せるか受信完了にするかを決める純粋関数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from app.ai_providers.errors import AIProviderError
from app.analysis.ai_provider_retry import is_unrecoverable_for_input
from app.analysis.embedding.domain.ready import EmbeddingReadyBuildRejected
from app.analysis.embedding.errors import EmbeddingError, EmbeddingFailureReason


@dataclass(frozen=True, slots=True)
class RetryEmbedding:
    """SQS の再配信に任せる Embedding の失敗。"""

    error: Exception


@dataclass(frozen=True, slots=True)
class NoRetryEmbedding:
    """再配信しても変わらないため受信完了にする Embedding の失敗。"""

    cause: EmbeddingReadyBuildRejected | AIProviderError | EmbeddingError


def classify_embedding_failure(exc: Exception) -> RetryEmbedding | NoRetryEmbedding:
    """同じ入力では変わらない失敗だけを受信完了にし、DB障害・想定外は再配信する。"""
    if isinstance(exc, AIProviderError):
        if is_unrecoverable_for_input(exc):
            return NoRetryEmbedding(exc)
        return RetryEmbedding(exc)
    if isinstance(exc, EmbeddingError):
        match exc.reason:
            case EmbeddingFailureReason.ARTICLE_MISSING:
                return NoRetryEmbedding(exc)
            case EmbeddingFailureReason.RESPONSE_INVALID:
                return RetryEmbedding(exc)
            case _:
                assert_never(exc.reason)
    return RetryEmbedding(exc)
