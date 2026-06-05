"""Stage 5 embedding の marker error と provider error adapter。"""

from __future__ import annotations

from typing import ClassVar

from app.analysis.ai_provider_errors import (
    AIProviderContentError,
    AIProviderError,
    AIProviderStateError,
)
from app.audit.domain.event import Stage
from app.audit.failure_projection import FailureAction, Retryability
from app.logfire.exceptions import VectorDomainError

# ---------------------------------------------------------------------------
# Layer 1 marker (Stage 5 task 層の dispatch 軸)
# ---------------------------------------------------------------------------


class EmbeddingError(VectorDomainError):
    """Stage 5 全例外の共通基底。直接の catch 対象にはしない。"""

    STAGE: ClassVar[Stage] = Stage.EMBEDDING


class EmbeddingRecoverableError(EmbeddingError):
    """再実行で回復しうる embedding 失敗。

    型が固定するのは retry 軸 (``RETRYABILITY``) だけ。原因軸 (``failure_kind`` =
    回復クラス / ``failure_reason`` = 詳細) は instance 値で持ち、provider error の
    ``FAILURE_MODE`` / ``reason`` から ``to_embedding_error`` が導出する。
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


class EmbeddingTerminalError(EmbeddingError):
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


# ---------------------------------------------------------------------------
# Layer 2-B (Stage 5 工程由来)
# ---------------------------------------------------------------------------


class EmbeddingResponseInvalidError(EmbeddingRecoverableError):
    """embedder 応答が embedding schema に合致しない。"""

    def __init__(self) -> None:
        super().__init__(
            code="embedding_response_invalid",
            failure_kind="ai_response_invalid",
            provider_error=None,
        )


# ---------------------------------------------------------------------------
# Layer 2-A ACL (provider 由来の詰め替え)
# ---------------------------------------------------------------------------
#
# ``EmbeddingService.execute()`` の boundary で ``to_embedding_error`` を呼ぶ。
# retry 軸 (Recoverable / Terminal) は provider の回復クラス
# (``FAILURE_MODE.retryable``) が一意に決め、原因軸 (failure_kind = mode 値 /
# failure_reason = reason 値) は同じ provider error から導出する (Stage 4
# ``map_provider_to_assessment`` と完全同形)。hold は marker 型ではなく handler が
# mode から導出する。
#
# state でも content でもない裸の ``AIProviderError`` を渡すと ``TypeError`` で
# fail-fast する。


def to_embedding_error(exc: AIProviderError) -> EmbeddingError:
    """provider 例外を Stage 5 marker に詰め替える。"""
    if not isinstance(exc, AIProviderStateError | AIProviderContentError):
        raise TypeError(f"unmapped provider error: {type(exc).__qualname__}")
    mode = exc.FAILURE_MODE
    marker = EmbeddingRecoverableError if mode.retryable else EmbeddingTerminalError
    return marker(
        code=exc.CODE,
        failure_kind=mode.value,
        failure_reason=exc.reason.value if exc.reason is not None else None,
        provider_error=exc,
    )
