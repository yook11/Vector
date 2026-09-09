"""既存Taskiq向けの失敗分類とServiceエラーの変換。"""

from __future__ import annotations

from typing import ClassVar

from app.analysis.ai_provider_errors import AIProviderError
from app.analysis.embedding.errors import EmbeddingError, EmbeddingFailureReason
from app.audit.failure_projection import FailureAction, Retryability
from app.logfire.exceptions import VectorDomainError


class EmbeddingTaskError(VectorDomainError):
    """既存Taskiqの監査・再試行に用いる失敗分類。"""


class EmbeddingRecoverableError(EmbeddingTaskError):
    """再実行で回復しうる embedding 失敗。

    型が固定するのは retry 軸 (``RETRYABILITY``) だけ。原因軸 (``failure_kind`` =
    回復クラス / ``failure_reason`` = 詳細) は instance 値で持ち、provider error の
    ``FAILURE_MODE`` / ``reason`` からTaskiq境界で導出する。
    ``failure_reason`` は forensic 用で ``SAFE_ATTRS`` に含めない。
    """

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    RETRYABILITY: ClassVar[Retryability] = Retryability.RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = None

    code: str
    failure_kind: str
    failure_reason: str | None
    provider_error: AIProviderError | None

    def __init__(
        self,
        *,
        code: str,
        failure_kind: str,
        failure_reason: str | None = None,
        provider_error: AIProviderError | None = None,
    ) -> None:
        super().__init__()
        self.code = code
        self.failure_kind = failure_kind
        self.failure_reason = failure_reason
        self.provider_error = provider_error


class EmbeddingTerminalError(EmbeddingTaskError):
    """再試行は無効で embedding を作らない Stage 5 失敗。

    型が固定するのは retry 軸 (``RETRYABILITY`` = NON_RETRYABLE) だけ。stage 全体を
    止めるか対象固有かは型では区別せず、handler が provider error の ``FAILURE_MODE``
    から hold を導出する。原因軸 (``failure_kind`` / ``failure_reason``) は
    ``EmbeddingRecoverableError`` と同形の instance 値。
    """

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)
    RETRYABILITY: ClassVar[Retryability] = Retryability.NON_RETRYABLE
    FAILURE_ACTION: ClassVar[FailureAction | None] = None

    code: str
    failure_kind: str
    failure_reason: str | None
    provider_error: AIProviderError | None

    def __init__(
        self,
        *,
        code: str,
        failure_kind: str,
        failure_reason: str | None = None,
        provider_error: AIProviderError | None = None,
    ) -> None:
        super().__init__()
        self.code = code
        self.failure_kind = failure_kind
        self.failure_reason = failure_reason
        self.provider_error = provider_error


def to_embedding_task_error(exc: BaseException) -> BaseException:
    """業務上の失敗理由を既存Taskiqの再試行分類へ対応付ける。"""
    if not isinstance(exc, EmbeddingError):
        return exc
    if exc.reason is EmbeddingFailureReason.PROVIDER_ERROR:
        provider = exc.provider_error
        if provider is None:
            raise TypeError("provider error is required")
        mode = provider.FAILURE_MODE
        marker = EmbeddingRecoverableError if mode.retryable else EmbeddingTerminalError
        result = marker(
            code=exc.code,
            failure_kind=mode.value,
            failure_reason=provider.reason.value
            if provider.reason is not None
            else None,
            provider_error=provider,
        )
    elif exc.reason is EmbeddingFailureReason.RESPONSE_INVALID:
        result = EmbeddingRecoverableError(
            code=exc.code, failure_kind="ai_response_invalid"
        )
    elif exc.reason is EmbeddingFailureReason.ARTICLE_MISSING:
        result = EmbeddingTerminalError(code=exc.code, failure_kind="target_missing")
    else:
        raise TypeError("unmapped embedding failure reason")
    result.__cause__ = exc
    return result
