"""Embeddingの業務上の失敗理由とプロバイダー例外の保持。"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from app.analysis.ai_provider_errors import (
    AIProviderContentError,
    AIProviderError,
    AIProviderStateError,
)
from app.logfire.exceptions import VectorDomainError


class EmbeddingFailureReason(StrEnum):
    """呼び出し側の再試行方針とは独立した失敗理由。"""

    ARTICLE_MISSING = "article_missing"
    RESPONSE_INVALID = "response_invalid"
    PROVIDER_ERROR = "provider_error"


class EmbeddingError(VectorDomainError):
    """失敗理由と元のプロバイダー例外を呼び出し元へ伝える。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)

    def __init__(
        self,
        *,
        reason: EmbeddingFailureReason,
        provider_error: AIProviderStateError | AIProviderContentError | None = None,
    ) -> None:
        if not isinstance(reason, EmbeddingFailureReason):
            raise TypeError("reason must be an EmbeddingFailureReason")
        if reason is EmbeddingFailureReason.PROVIDER_ERROR:
            if not isinstance(
                provider_error, AIProviderStateError | AIProviderContentError
            ):
                raise TypeError("provider_error must be a classified AI provider error")
        elif provider_error is not None:
            raise TypeError("provider_error requires PROVIDER_ERROR reason")
        super().__init__()
        self.reason = reason
        self.provider_error = provider_error

    @property
    def code(self) -> str:
        """既存の観測コードを失敗理由から導出する。"""
        if self.provider_error is not None:
            return self.provider_error.CODE
        if self.reason is EmbeddingFailureReason.ARTICLE_MISSING:
            return "embedding_analyzed_article_missing"
        return "embedding_response_invalid"


class EmbeddingAnalyzedArticleMissingError(EmbeddingError):
    """処理開始時またはベクトル保存時に対象の分析記事が存在しない。"""

    def __init__(self) -> None:
        super().__init__(reason=EmbeddingFailureReason.ARTICLE_MISSING)


class EmbeddingResponseInvalidError(EmbeddingError):
    """埋め込み応答がベクトルの契約を満たさない。"""

    def __init__(self) -> None:
        super().__init__(reason=EmbeddingFailureReason.RESPONSE_INVALID)


def to_embedding_error(exc: AIProviderError) -> EmbeddingError:
    """プロバイダー例外を保持し、Serviceの失敗理由を付与する。"""
    if not isinstance(exc, AIProviderStateError | AIProviderContentError):
        raise TypeError(f"unmapped provider error: {type(exc).__qualname__}")
    return EmbeddingError(
        reason=EmbeddingFailureReason.PROVIDER_ERROR, provider_error=exc
    )
