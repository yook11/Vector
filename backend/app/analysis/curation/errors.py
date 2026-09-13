"""Curationの失敗理由とプロバイダー例外を保持する。"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from app.ai_providers.errors import (
    AIProviderContentError,
    AIProviderError,
    AIProviderStateError,
)
from app.logfire.exceptions import VectorDomainError


class CurationFailureReason(StrEnum):
    """呼び出し側の再試行・記事削除方針から独立した失敗理由。"""

    PROVIDER_ERROR = "provider_error"
    RESPONSE_INVALID = "response_invalid"


class CurationError(VectorDomainError):
    """失敗理由と分類済みのプロバイダー例外を呼び出し元へ伝える。"""

    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("code",)

    def __init__(
        self,
        *,
        reason: CurationFailureReason,
        provider_error: AIProviderStateError | AIProviderContentError | None = None,
    ) -> None:
        if not isinstance(reason, CurationFailureReason):
            raise TypeError("reason must be CurationFailureReason")
        if reason is CurationFailureReason.PROVIDER_ERROR:
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
        """観測用のコードには安全な種別ラベルだけを用いる。"""
        if self.provider_error is not None:
            return self.provider_error.CODE
        return "extraction_response_invalid"


class CurationResponseInvalidError(CurationError):
    """既存のコードと引数なし契約でAI応答不正を表す。"""

    def __init__(self) -> None:
        super().__init__(reason=CurationFailureReason.RESPONSE_INVALID)


def to_curation_error(exc: AIProviderError) -> CurationError:
    """プロバイダー例外を同じインスタンスのまま保持する。"""
    if not isinstance(exc, AIProviderStateError | AIProviderContentError):
        raise TypeError(f"unmapped provider error: {type(exc).__qualname__}")
    return CurationError(
        reason=CurationFailureReason.PROVIDER_ERROR, provider_error=exc
    )
