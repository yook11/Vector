"""工程に依存しないAIプロバイダー例外の型と原因。

クラスは失敗がどこで判明したか、reason は何が起きたかを表す。
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from app.http.errors import HttpResponseError, HttpTransportError
from app.http.failure import HttpTransportFailureReason
from app.shared.errors import ApplicationError, ApplicationErrorValue


class AIProviderError(ApplicationError):
    """入力値を含めない説明と分類済みの診断を保持するプロバイダー例外。"""

    CODE: ClassVar[str]
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーの処理に失敗しました"
    reason: (
        AIProviderNotSentReason
        | HttpTransportFailureReason
        | AIProviderResponseReason
        | AIProviderResultReason
    )

    def __init__(
        self,
        message: str | None = None,
        *,
        reason: AIProviderNotSentReason
        | HttpTransportFailureReason
        | AIProviderResponseReason
        | AIProviderResultReason,
    ) -> None:
        # どこで判明したかはサブクラスが表すので、基底のままでは作らせない。
        if type(self) is AIProviderError:
            raise TypeError("AIProviderError cannot be instantiated directly")
        if message is not None and not isinstance(message, str):
            raise TypeError("message must be a string or None")
        if not isinstance(reason, StrEnum):
            raise TypeError("reason must be a StrEnum member")
        details: dict[str, ApplicationErrorValue] = {}
        code = getattr(type(self), "CODE", None)
        if code is not None:
            details["code"] = code
        details["reason"] = reason.value
        super().__init__(
            self.DEFAULT_MESSAGE if message is None else message,
            details=details,
        )
        self.reason = reason


class AIProviderNotSentReason(StrEnum):
    NOT_CONFIGURED = "not_configured"
    HOST_BLOCKED = "host_blocked"
    """宛先の方針がプロバイダーへの通信を拒否した。"""


class AIProviderNotSentError(AIProviderError):
    """設定の不足や宛先の方針により、リクエストを送らなかった。"""

    CODE: ClassVar[str] = "ai_provider_not_sent_error"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーへのリクエストを送信しませんでした"
    reason: AIProviderNotSentReason

    def __init__(
        self,
        message: str | None = None,
        *,
        reason: AIProviderNotSentReason,
    ) -> None:
        if not isinstance(reason, AIProviderNotSentReason):
            raise TypeError("reason must be an AIProviderNotSentReason")
        super().__init__(message, reason=reason)


class AIProviderTransportError(AIProviderError):
    """通信が完了せず、プロバイダーの応答を受け取れなかった。"""

    CODE: ClassVar[str] = "ai_provider_transport_error"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーとの通信に失敗しました"
    reason: HttpTransportFailureReason

    def __init__(
        self, message: str | None = None, *, http_error: HttpTransportError
    ) -> None:
        if not isinstance(http_error, HttpTransportError):
            raise TypeError("http_error must be an HttpTransportError")
        super().__init__(message, reason=http_error.failure.reason)
        self.http_error = http_error


class AIProviderResponseReason(StrEnum):
    AUTH = "auth"
    LEAKED_API_KEY = "leaked_api_key"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    FAILED_PRECONDITION = "failed_precondition"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    """日あたりなど、時間で戻る利用枠を使い切った。"""
    SERVER_ERROR = "server_error"
    INPUT_TOO_LONG = "input_too_long"
    INPUT_BLOCKED = "input_blocked"


class AIProviderResponseError(AIProviderError):
    """プロバイダーが非成功応答を返した。"""

    CODE: ClassVar[str] = "ai_provider_response_error"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーが失敗の応答を返しました"
    reason: AIProviderResponseReason

    def __init__(
        self,
        message: str | None = None,
        *,
        reason: AIProviderResponseReason,
        http_error: HttpResponseError,
    ) -> None:
        if not isinstance(reason, AIProviderResponseReason):
            raise TypeError("reason must be an AIProviderResponseReason")
        if not isinstance(http_error, HttpResponseError):
            raise TypeError("http_error must be an HttpResponseError")
        super().__init__(message, reason=reason)
        self.http_error = http_error


class AIProviderResultReason(StrEnum):
    INPUT_BLOCKED = "input_blocked"
    OUTPUT_BLOCKED_SAFETY = "output_blocked_safety"
    OUTPUT_BLOCKED_RECITATION = "output_blocked_recitation"
    OUTPUT_BLOCKED_BLOCKLIST = "output_blocked_blocklist"
    OUTPUT_BLOCKED_PROHIBITED_CONTENT = "output_blocked_prohibited_content"
    OUTPUT_BLOCKED_SPII = "output_blocked_spii"
    OUTPUT_TRUNCATED = "output_truncated"
    STREAM_INCOMPLETE = "stream_incomplete"
    """ストリームが終了の理由を受け取らずに終わった。"""
    EMBEDDINGS_EMPTY = "embeddings_empty"
    EMBEDDING_VALUES_MISSING = "embedding_values_missing"
    EMBEDDING_COUNT_MISMATCH = "embedding_count_mismatch"
    RESPONSE_UNPARSEABLE = "response_unparseable"


class AIProviderResultError(AIProviderError):
    """成功応答を受け取ったが、中身を使えなかった。"""

    CODE: ClassVar[str] = "ai_provider_result_error"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーの生成結果を利用できませんでした"
    reason: AIProviderResultReason

    def __init__(
        self,
        message: str | None = None,
        *,
        reason: AIProviderResultReason,
    ) -> None:
        if not isinstance(reason, AIProviderResultReason):
            raise TypeError("reason must be an AIProviderResultReason")
        super().__init__(message, reason=reason)


# 未知の直接サブクラスは分類済みの失敗として扱わない。
CLASSIFIED_AI_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderNotSentError,
    AIProviderTransportError,
    AIProviderResponseError,
    AIProviderResultError,
)
