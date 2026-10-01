"""DeepSeek (OpenAI SDK) の例外を AIProvider*Error に分類する。

finish_reason による拒否の判定は、各工程の adapter が持つ。
"""

from __future__ import annotations

from enum import StrEnum

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableEntityError,
)
from openai import RateLimitError as OpenAIRateLimitError

from app.ai_providers.errors import (
    AIProviderConfigurationError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)
from app.http.destination_policy import HostBlockedError


class DeepSeekStateReason(StrEnum):
    """DeepSeek と通信の状態の理由。値は監査の failure_reason に残る。"""

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    HOST_BLOCKED = "host_blocked"
    AUTH = "auth"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    RATE_LIMITED = "rate_limited"
    OUTPUT_TOKEN_LIMIT_REACHED = "output_token_limit_reached"  # noqa: S105
    BAD_REQUEST = "bad_request"
    UNPROCESSABLE = "unprocessable"
    SERVER_ERROR = "server_error"
    NOT_CONFIGURED = "not_configured"


def translate_deepseek_error(exc: Exception) -> Exception:
    """OpenAI SDK の例外を分類し、分類できなければ元の例外を返す。

    SDK の生の message は PII を含みうるので、固定の文言だけを使う。
    """
    # SDK は送信中の例外を APIConnectionError に包むので、宛先の拒否は原因で見分ける。
    if isinstance(exc, APIConnectionError) and isinstance(
        exc.__cause__, HostBlockedError
    ):
        return AIProviderNetworkError(
            "AIプロバイダーへの通信が宛先の方針で拒否されました",
            reason=DeepSeekStateReason.HOST_BLOCKED,
        )
    # APITimeoutError は APIConnectionError の子クラスなので先に判定する。
    if isinstance(exc, APITimeoutError):
        return AIProviderNetworkError(
            "AIプロバイダーとの通信がタイムアウトしました",
            reason=DeepSeekStateReason.TIMEOUT,
        )
    if isinstance(exc, APIConnectionError):
        return AIProviderNetworkError(
            "AIプロバイダーに接続できませんでした",
            reason=DeepSeekStateReason.CONNECTION,
        )
    if isinstance(exc, TimeoutError):
        return AIProviderNetworkError(
            "AIプロバイダーとの通信がタイムアウトしました",
            reason=DeepSeekStateReason.TIMEOUT,
        )
    if isinstance(exc, (ConnectionError, OSError)):
        return AIProviderNetworkError(
            "AIプロバイダーに接続できませんでした",
            reason=DeepSeekStateReason.CONNECTION,
        )

    if isinstance(exc, AuthenticationError):
        return AIProviderConfigurationError(
            "AIプロバイダーの認証に失敗しました", reason=DeepSeekStateReason.AUTH
        )
    if isinstance(exc, PermissionDeniedError):
        return AIProviderConfigurationError(
            "AIプロバイダーへのアクセス権限がありません",
            reason=DeepSeekStateReason.PERMISSION_DENIED,
        )
    if isinstance(exc, NotFoundError):
        return AIProviderConfigurationError(
            "AIプロバイダーの要求先が見つかりません",
            reason=DeepSeekStateReason.NOT_FOUND,
        )

    # 402 (残高不足) は専用の SDK 例外がないので、RateLimitError より先に見る。
    if isinstance(exc, APIStatusError) and exc.status_code == 402:
        return AIProviderInsufficientBalanceError(
            "AIプロバイダーの利用残高が不足しています",
            reason=DeepSeekStateReason.INSUFFICIENT_BALANCE,
        )

    if isinstance(exc, OpenAIRateLimitError):
        return AIProviderRateLimitedError(
            "AIプロバイダーの呼び出し頻度の上限に達しました",
            reason=DeepSeekStateReason.RATE_LIMITED,
        )

    if isinstance(exc, BadRequestError):
        return AIProviderRequestInvalidError(
            "AIプロバイダーがリクエストを不正と判定しました",
            reason=DeepSeekStateReason.BAD_REQUEST,
        )
    if isinstance(exc, UnprocessableEntityError):
        return AIProviderRequestInvalidError(
            "AIプロバイダーがリクエストを処理できませんでした",
            reason=DeepSeekStateReason.UNPROCESSABLE,
        )

    if isinstance(exc, InternalServerError):
        return AIProviderServiceUnavailableError(
            "AIプロバイダー内部でサーバーエラーが発生しました",
            reason=DeepSeekStateReason.SERVER_ERROR,
        )

    if isinstance(exc, APIStatusError) and 500 <= exc.status_code < 600:
        return AIProviderServiceUnavailableError(
            "AIプロバイダー内部でサーバーエラーが発生しました",
            reason=DeepSeekStateReason.SERVER_ERROR,
        )

    return exc
