"""工程に依存しないAIプロバイダー例外の型と原因。

クラスは失敗がどこで判明したか、reason は何が起きたか、recovery は回復の条件を表す。
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from app.http.failure import HttpTransportFailure, HttpTransportFailureReason
from app.shared.errors import ApplicationError, ApplicationErrorValue


class AIProviderRecovery(StrEnum):
    """失敗から回復するために必要な条件。"""

    OPERATOR_ACTION_REQUIRED = "operator_action_required"
    """設定・残高・実装を人が直すまで、どの入力でも失敗する。"""
    RECOVERS_AFTER_WAIT = "recovers_after_wait"
    """流量や利用枠の制限で、時間を置けば回復する。"""
    MAY_RECOVER_ON_RETRY = "may_recover_on_retry"
    """一時的な障害や出力の揺らぎで、再試行すれば成功しうる。"""
    NOT_RECOVERABLE_FOR_INPUT = "not_recoverable_for_input"
    """この入力が原因で再試行しても成功しないが、他の入力は通る。"""


class AIProviderError(ApplicationError):
    """入力値を含めない説明と分類済みの診断を保持するプロバイダー例外。"""

    CODE: ClassVar[str]
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーの処理に失敗しました"
    reason: StrEnum | None

    def __init__(
        self, message: str | None = None, *, reason: StrEnum | None = None
    ) -> None:
        if message is not None and not isinstance(message, str):
            raise TypeError("message must be a string or None")
        if reason is not None and not isinstance(reason, StrEnum):
            raise TypeError("reason must be a StrEnum member or None")
        details: dict[str, ApplicationErrorValue] = {}
        code = getattr(type(self), "CODE", None)
        if code is not None:
            details["code"] = code
        if reason is not None:
            details["reason"] = reason.value
        super().__init__(
            self.DEFAULT_MESSAGE if message is None else message,
            details=details or None,
        )
        self.reason = reason


class AIProviderRequestNotSentReason(StrEnum):
    NOT_CONFIGURED = "not_configured"
    HOST_BLOCKED = "host_blocked"
    """宛先の方針がプロバイダーへの通信を拒否した。"""


class AIProviderRequestNotSentError(AIProviderError):
    """設定の不足や宛先の方針により、リクエストを送らなかった。"""

    CODE: ClassVar[str] = "ai_provider_request_not_sent"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーへのリクエストを送信しませんでした"
    reason: AIProviderRequestNotSentReason

    def __init__(
        self,
        message: str | None = None,
        *,
        reason: AIProviderRequestNotSentReason,
    ) -> None:
        if not isinstance(reason, AIProviderRequestNotSentReason):
            raise TypeError("reason must be an AIProviderRequestNotSentReason")
        super().__init__(message, reason=reason)

    @property
    def recovery(self) -> AIProviderRecovery:
        return AIProviderRecovery.OPERATOR_ACTION_REQUIRED


class AIProviderTransportError(AIProviderError):
    """通信が完了せず、プロバイダーの応答を受け取れなかった。"""

    CODE: ClassVar[str] = "ai_provider_transport_failed"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーとの通信に失敗しました"
    reason: HttpTransportFailureReason

    def __init__(
        self, message: str | None = None, *, transport: HttpTransportFailure
    ) -> None:
        if not isinstance(transport, HttpTransportFailure):
            raise TypeError("transport must be an HttpTransportFailure")
        super().__init__(message, reason=transport.reason)
        self.transport = transport

    @property
    def recovery(self) -> AIProviderRecovery:
        # proxy の 4xx は、送信先の許可リストなどこちらの設定による拒否である。
        proxy_status = self.transport.proxy_status
        if (
            self.transport.reason is HttpTransportFailureReason.PROXY
            and proxy_status is not None
            and 400 <= proxy_status < 500
        ):
            return AIProviderRecovery.OPERATOR_ACTION_REQUIRED
        return AIProviderRecovery.MAY_RECOVER_ON_RETRY


class AIProviderErrorResponseReason(StrEnum):
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


_ERROR_RESPONSE_RECOVERY: dict[AIProviderErrorResponseReason, AIProviderRecovery] = {
    AIProviderErrorResponseReason.AUTH: AIProviderRecovery.OPERATOR_ACTION_REQUIRED,
    AIProviderErrorResponseReason.LEAKED_API_KEY: (
        AIProviderRecovery.OPERATOR_ACTION_REQUIRED
    ),
    AIProviderErrorResponseReason.PERMISSION_DENIED: (
        AIProviderRecovery.OPERATOR_ACTION_REQUIRED
    ),
    AIProviderErrorResponseReason.NOT_FOUND: (
        AIProviderRecovery.OPERATOR_ACTION_REQUIRED
    ),
    AIProviderErrorResponseReason.FAILED_PRECONDITION: (
        AIProviderRecovery.OPERATOR_ACTION_REQUIRED
    ),
    AIProviderErrorResponseReason.INSUFFICIENT_BALANCE: (
        AIProviderRecovery.OPERATOR_ACTION_REQUIRED
    ),
    AIProviderErrorResponseReason.INVALID_REQUEST: (
        AIProviderRecovery.OPERATOR_ACTION_REQUIRED
    ),
    AIProviderErrorResponseReason.RATE_LIMITED: AIProviderRecovery.RECOVERS_AFTER_WAIT,
    AIProviderErrorResponseReason.QUOTA_EXHAUSTED: (
        AIProviderRecovery.RECOVERS_AFTER_WAIT
    ),
    AIProviderErrorResponseReason.SERVER_ERROR: AIProviderRecovery.MAY_RECOVER_ON_RETRY,
    AIProviderErrorResponseReason.INPUT_TOO_LONG: (
        AIProviderRecovery.NOT_RECOVERABLE_FOR_INPUT
    ),
    AIProviderErrorResponseReason.INPUT_BLOCKED: (
        AIProviderRecovery.NOT_RECOVERABLE_FOR_INPUT
    ),
}


class AIProviderErrorResponseError(AIProviderError):
    """プロバイダーが成功以外の応答を返した。"""

    CODE: ClassVar[str] = "ai_provider_error_response"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーが失敗の応答を返しました"
    reason: AIProviderErrorResponseReason

    def __init__(
        self,
        message: str | None = None,
        *,
        reason: AIProviderErrorResponseReason,
        status_code: int,
    ) -> None:
        if not isinstance(reason, AIProviderErrorResponseReason):
            raise TypeError("reason must be an AIProviderErrorResponseReason")
        if not isinstance(status_code, int) or isinstance(status_code, bool):
            raise TypeError("status_code must be an int")
        super().__init__(message, reason=reason)
        self.status_code = status_code

    @property
    def recovery(self) -> AIProviderRecovery:
        return _ERROR_RESPONSE_RECOVERY[self.reason]


class AIProviderGenerationReason(StrEnum):
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


_GENERATION_RECOVERY: dict[AIProviderGenerationReason, AIProviderRecovery] = {
    AIProviderGenerationReason.INPUT_BLOCKED: (
        AIProviderRecovery.NOT_RECOVERABLE_FOR_INPUT
    ),
    AIProviderGenerationReason.OUTPUT_BLOCKED_SAFETY: (
        AIProviderRecovery.NOT_RECOVERABLE_FOR_INPUT
    ),
    AIProviderGenerationReason.OUTPUT_BLOCKED_RECITATION: (
        AIProviderRecovery.NOT_RECOVERABLE_FOR_INPUT
    ),
    AIProviderGenerationReason.OUTPUT_BLOCKED_BLOCKLIST: (
        AIProviderRecovery.NOT_RECOVERABLE_FOR_INPUT
    ),
    AIProviderGenerationReason.OUTPUT_BLOCKED_PROHIBITED_CONTENT: (
        AIProviderRecovery.NOT_RECOVERABLE_FOR_INPUT
    ),
    AIProviderGenerationReason.OUTPUT_BLOCKED_SPII: (
        AIProviderRecovery.NOT_RECOVERABLE_FOR_INPUT
    ),
    AIProviderGenerationReason.OUTPUT_TRUNCATED: (
        AIProviderRecovery.MAY_RECOVER_ON_RETRY
    ),
    AIProviderGenerationReason.STREAM_INCOMPLETE: (
        AIProviderRecovery.MAY_RECOVER_ON_RETRY
    ),
    AIProviderGenerationReason.EMBEDDINGS_EMPTY: (
        AIProviderRecovery.MAY_RECOVER_ON_RETRY
    ),
    AIProviderGenerationReason.EMBEDDING_VALUES_MISSING: (
        AIProviderRecovery.MAY_RECOVER_ON_RETRY
    ),
    AIProviderGenerationReason.EMBEDDING_COUNT_MISMATCH: (
        AIProviderRecovery.MAY_RECOVER_ON_RETRY
    ),
    AIProviderGenerationReason.RESPONSE_UNPARSEABLE: (
        AIProviderRecovery.MAY_RECOVER_ON_RETRY
    ),
}


class AIProviderGenerationError(AIProviderError):
    """成功の応答を受け取ったが、生成結果を使えなかった。"""

    CODE: ClassVar[str] = "ai_provider_generation_unusable"
    DEFAULT_MESSAGE: ClassVar[str] = "AIプロバイダーの生成結果を利用できませんでした"
    reason: AIProviderGenerationReason

    def __init__(
        self,
        message: str | None = None,
        *,
        reason: AIProviderGenerationReason,
    ) -> None:
        if not isinstance(reason, AIProviderGenerationReason):
            raise TypeError("reason must be an AIProviderGenerationReason")
        super().__init__(message, reason=reason)

    @property
    def recovery(self) -> AIProviderRecovery:
        return _GENERATION_RECOVERY[self.reason]


# 基底型と未知の直接サブクラスは分類済みの失敗として扱わない。
CLASSIFIED_AI_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderRequestNotSentError,
    AIProviderTransportError,
    AIProviderErrorResponseError,
    AIProviderGenerationError,
)
