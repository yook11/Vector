"""DeepSeek (OpenAI SDK) 例外を AIProvider*Error に分類する共通 translator。

DeepSeek は OpenAI SDK を ``base_url=https://api.deepseek.com/beta`` 経由で利用する。
SDK 例外 / HTTP status を provider 中立な ``AIProvider*Error`` 階層に翻訳し、adapter の
責務を I/O 駆動に絞る (Gemini の ``translate_gemini_error`` と対称)。

責任の境界:
- ``translate_deepseek_error``: SDK exception / HTTP status を見た分類のみ。
- finish_reason 由来の content 拒否は持ち込まない (Gemini 同様 adapter の ``_call_api``
  責務)。

reason 語彙の所有:
- ``DeepSeekStateReason``: provider / 環境状態の具体理由 (timeout / auth / 402 等)。
  translator の各分岐 + adapter local 検知 (未設定) に対応する。reason は audit の
  ``failure_reason`` に焼かれる種別ラベル (PII-free)。

DeepSeek 固有:
- HTTP 402 (Insufficient Balance) は OpenAI 本家にない概念。専用 SDK 例外がないので
  ``APIStatusError.status_code`` で判定し、``OpenAIRateLimitError`` 等より先に評価する。
- translator 名は provider 軸 (``deepseek_``) とする
  (OpenAI 本家にない 402 固有 semantics を含むため)。
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

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)


class DeepSeekStateReason(StrEnum):
    """DeepSeek (OpenAI SDK) provider / 環境状態の具体理由 (PII-free な種別ラベル)。

    ``translate_deepseek_error`` の各分岐 (SDK 例外種別 / HTTP status) と adapter
    local 検知 (未設定) に対応する。値は audit の ``failure_reason`` に焼かれ、
    ``outcome_code`` (= CODE) より細かい原因を残す。
    """

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    AUTH = "auth"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    RATE_LIMITED = "rate_limited"
    BAD_REQUEST = "bad_request"
    UNPROCESSABLE = "unprocessable"
    SERVER_ERROR = "server_error"
    NOT_CONFIGURED = "not_configured"


def translate_deepseek_error(exc: Exception) -> Exception:
    """OpenAI SDK 例外を ``AIProvider*Error`` 階層に翻訳する。

    HTTP 402 (Insufficient Balance) は専用 SDK 例外がないので
    ``APIStatusError.status_code`` で判定し、``OpenAIRateLimitError`` 等の専用
    サブクラスより先に評価する。

    マップできなければ ``exc`` をそのまま return (caller である ``_call_once`` が
    bare re-raise する規約)。
    """
    # network 系。SDK 生 message は provider error detail に載せない。
    # timeout / connection を reason で区別する (APITimeoutError は
    # APIConnectionError の subclass なので先に評価する)。
    if isinstance(exc, APITimeoutError):
        return AIProviderNetworkError(reason=DeepSeekStateReason.TIMEOUT)
    if isinstance(exc, APIConnectionError):
        return AIProviderNetworkError(reason=DeepSeekStateReason.CONNECTION)
    if isinstance(exc, TimeoutError):
        return AIProviderNetworkError(reason=DeepSeekStateReason.TIMEOUT)
    if isinstance(exc, (ConnectionError, OSError)):
        return AIProviderNetworkError(reason=DeepSeekStateReason.CONNECTION)

    if isinstance(exc, AuthenticationError):
        return AIProviderConfigurationError(reason=DeepSeekStateReason.AUTH)
    if isinstance(exc, PermissionDeniedError):
        return AIProviderConfigurationError(
            reason=DeepSeekStateReason.PERMISSION_DENIED
        )
    if isinstance(exc, NotFoundError):
        return AIProviderConfigurationError(reason=DeepSeekStateReason.NOT_FOUND)

    # HTTP 402 を OpenAIRateLimitError より先に評価 (DeepSeek 固有)
    if isinstance(exc, APIStatusError) and exc.status_code == 402:
        return AIProviderInsufficientBalanceError(
            reason=DeepSeekStateReason.INSUFFICIENT_BALANCE
        )

    if isinstance(exc, OpenAIRateLimitError):
        return AIProviderRateLimitedError(reason=DeepSeekStateReason.RATE_LIMITED)

    if isinstance(exc, BadRequestError):
        return AIProviderRequestInvalidError(reason=DeepSeekStateReason.BAD_REQUEST)
    if isinstance(exc, UnprocessableEntityError):
        return AIProviderRequestInvalidError(reason=DeepSeekStateReason.UNPROCESSABLE)

    if isinstance(exc, InternalServerError):
        return AIProviderServiceUnavailableError(
            reason=DeepSeekStateReason.SERVER_ERROR
        )

    if isinstance(exc, APIStatusError) and 500 <= exc.status_code < 600:
        return AIProviderServiceUnavailableError(
            reason=DeepSeekStateReason.SERVER_ERROR
        )

    return exc  # bare re-raise (UNKNOWN)
