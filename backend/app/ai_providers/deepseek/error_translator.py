"""DeepSeek (OpenAI SDK) の例外を AIProvider*Error に分類する。

finish_reason による拒否の判定は、各工程の adapter が持つ。
"""

from __future__ import annotations

from datetime import UTC, datetime

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
    AIProviderNotSentError,
    AIProviderNotSentReason,
    AIProviderResponseError,
    AIProviderResponseReason,
    AIProviderTransportError,
)
from app.http.destination_policy import HostBlockedError
from app.http.error_mapping import (
    http_response_error_from_status,
    http_transport_error_from_exception,
)
from app.http.errors import HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)


def _transport_error(exc: APIConnectionError) -> HttpTransportError:
    """SDK が包んだ通信の例外を分類し、包む前の例外が不明なら段階を不明とする。"""
    cause = exc.__cause__
    http_error = (
        http_transport_error_from_exception(cause)
        if isinstance(cause, Exception)
        else None
    )
    if http_error is not None:
        return http_error
    reason = (
        HttpTransportFailureReason.TIMEOUT
        if isinstance(exc, APITimeoutError)
        else HttpTransportFailureReason.UNKNOWN
    )
    return HttpTransportError(
        failure=HttpTransportFailure(HttpTransportStage.UNKNOWN, reason)
    )


def translate_deepseek_error(exc: Exception) -> Exception:
    """OpenAI SDK の例外を分類し、分類できなければ元の例外を返す。

    SDK の生の message は PII を含みうるので、固定の文言だけを使う。
    """
    # SDK は送信中の例外を APIConnectionError に包むので、宛先の拒否は原因で見分ける。
    if isinstance(exc, APIConnectionError):
        if isinstance(exc.__cause__, HostBlockedError):
            return AIProviderNotSentError(
                "AIプロバイダーへの通信が宛先の方針で拒否されました",
                reason=AIProviderNotSentReason.HOST_BLOCKED,
            )
        return AIProviderTransportError(http_error=_transport_error(exc))

    if not isinstance(exc, APIStatusError):
        return exc
    status_code = exc.status_code
    # 変換器は SDK の例外を捕まえた直後に呼ばれるので、今の時刻を受信時刻とする。
    http_error = http_response_error_from_status(
        status_code, response=exc.response, received_at=datetime.now(UTC)
    )

    if isinstance(exc, AuthenticationError):
        return AIProviderResponseError(
            "AIプロバイダーの認証に失敗しました",
            reason=AIProviderResponseReason.AUTH,
            http_error=http_error,
        )
    if isinstance(exc, PermissionDeniedError):
        return AIProviderResponseError(
            "AIプロバイダーへのアクセス権限がありません",
            reason=AIProviderResponseReason.PERMISSION_DENIED,
            http_error=http_error,
        )
    if isinstance(exc, NotFoundError):
        return AIProviderResponseError(
            "AIプロバイダーの要求先が見つかりません",
            reason=AIProviderResponseReason.NOT_FOUND,
            http_error=http_error,
        )

    # 402 (残高不足) は専用の SDK 例外がないので、RateLimitError より先に見る。
    if status_code == 402:
        return AIProviderResponseError(
            "AIプロバイダーの利用残高が不足しています",
            reason=AIProviderResponseReason.INSUFFICIENT_BALANCE,
            http_error=http_error,
        )

    if isinstance(exc, OpenAIRateLimitError):
        return AIProviderResponseError(
            "AIプロバイダーの呼び出し頻度の上限に達しました",
            reason=AIProviderResponseReason.RATE_LIMITED,
            http_error=http_error,
        )

    if isinstance(exc, BadRequestError):
        return AIProviderResponseError(
            "AIプロバイダーがリクエストを不正と判定しました",
            reason=AIProviderResponseReason.INVALID_REQUEST,
            http_error=http_error,
        )
    if isinstance(exc, UnprocessableEntityError):
        return AIProviderResponseError(
            "AIプロバイダーがリクエストを処理できませんでした",
            reason=AIProviderResponseReason.INVALID_REQUEST,
            http_error=http_error,
        )

    if isinstance(exc, InternalServerError) or 500 <= status_code < 600:
        return AIProviderResponseError(
            "AIプロバイダー内部でサーバーエラーが発生しました",
            reason=AIProviderResponseReason.SERVER_ERROR,
            http_error=http_error,
        )

    return exc
