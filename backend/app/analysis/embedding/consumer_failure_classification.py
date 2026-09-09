"""Consumerの失敗を監査・監視・通知の情報へ投影する純粋関数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.analysis.ai_provider_exhaustion import (
    ExhaustedProviderError,
    exhausted_provider_error,
)
from app.analysis.ai_provider_outcome import is_infra_provider_error
from app.analysis.embedding.errors import EmbeddingError, EmbeddingFailureReason
from app.audit.failure_projection import (
    FailureProjection,
    Retryability,
    project_db_failure,
    unknown_failure_projection,
)


@dataclass(frozen=True, slots=True)
class EmbeddingFailureClassification:
    """再配信の判断を含まない、失敗後処理に必要な情報。"""

    audit: FailureProjection
    outcome: Literal["failed", "infra_error"]
    provider_exhaustion: ExhaustedProviderError | None = None


def classify_embedding_failure(exc: Exception) -> EmbeddingFailureClassification:
    """Serviceの失敗理由を既存の監査・監視分類へ対応付ける。"""
    if isinstance(exc, EmbeddingError):
        if exc.reason is EmbeddingFailureReason.PROVIDER_ERROR:
            provider_error = exc.provider_error
            if provider_error is None:
                raise TypeError("provider error is required")
            mode = provider_error.FAILURE_MODE
            return EmbeddingFailureClassification(
                audit=FailureProjection(
                    code=exc.code,
                    failure_kind=mode.value,
                    failure_reason=provider_error.reason.value
                    if provider_error.reason is not None
                    else None,
                    retryability=Retryability.RETRYABLE
                    if mode.retryable
                    else Retryability.NON_RETRYABLE,
                    failure_action=None,
                ),
                outcome="infra_error"
                if is_infra_provider_error(provider_error)
                else "failed",
                provider_exhaustion=exhausted_provider_error(provider_error),
            )
        if exc.reason is EmbeddingFailureReason.ARTICLE_MISSING:
            return EmbeddingFailureClassification(
                audit=FailureProjection(
                    code=exc.code,
                    failure_kind="target_missing",
                    retryability=Retryability.NON_RETRYABLE,
                    failure_action=None,
                ),
                outcome="failed",
            )
        if exc.reason is EmbeddingFailureReason.RESPONSE_INVALID:
            return EmbeddingFailureClassification(
                audit=FailureProjection(
                    code=exc.code,
                    failure_kind="ai_response_invalid",
                    retryability=Retryability.RETRYABLE,
                    failure_action=None,
                ),
                outcome="failed",
            )
        raise TypeError("unmapped embedding failure reason")

    db_failure = project_db_failure(exc)
    if db_failure is not None:
        return EmbeddingFailureClassification(audit=db_failure, outcome="infra_error")
    return EmbeddingFailureClassification(
        audit=unknown_failure_projection(), outcome="failed"
    )
