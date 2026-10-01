"""Gemini SDK の例外を AIProvider*Error に分類する。

工程によらず同じ障害を同じ例外にするため、SDK の例外を見る分類はここに集める。
finish_reason や応答の形の検証は、各工程の adapter が持つ。
"""

from __future__ import annotations

from enum import StrEnum

from google.genai import errors as genai_errors

from app.ai_providers.errors import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    AIProviderUsageLimitExhaustedError,
)
from app.http.destination_policy import HostBlockedError
from app.http.failure import HttpTransportFailureReason, classify_httpx


class GeminiContentRejectionReason(StrEnum):
    """Gemini が入出力を拒否した理由。値は監査の failure_reason に残る。"""

    SAFETY = "safety"
    RECITATION = "recitation"
    BLOCKLIST = "blocklist"
    PROHIBITED_CONTENT = "prohibited_content"
    SPII = "spii"
    INPUT_BLOCKED = "input_blocked"
    CONTEXT_LENGTH = "context_length"


class GeminiStateReason(StrEnum):
    """Gemini と通信の状態の理由。値は監査の failure_reason に残る。"""

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    HOST_BLOCKED = "host_blocked"
    SERVER_ERROR = "server_error"
    LEAKED_API_KEY = "leaked_api_key"
    AUTH = "auth"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    FAILED_PRECONDITION = "failed_precondition"
    INVALID_ARGUMENT = "invalid_argument"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    # 以下は各 adapter が検知する。
    NOT_CONFIGURED = "not_configured"
    STREAM_TRUNCATED = "stream_truncated"
    EMPTY_EMBEDDINGS = "empty_embeddings"
    MISSING_VALUES = "missing_values"
    EMBEDDING_COUNT_MISMATCH = "embedding_count_mismatch"
    OUTPUT_TOKEN_LIMIT_REACHED = "output_token_limit_reached"  # noqa: S105


_CONFIG_REASON_MESSAGES: dict[GeminiStateReason, str] = {
    GeminiStateReason.AUTH: "AIプロバイダーの認証に失敗しました",
    GeminiStateReason.PERMISSION_DENIED: "AIプロバイダーへのアクセス権限がありません",
    GeminiStateReason.NOT_FOUND: "AIプロバイダーの要求先が見つかりません",
    GeminiStateReason.FAILED_PRECONDITION: (
        "AIプロバイダーの利用に必要な前提条件が満たされていません"
    ),
}


_FINISH_REASON_TO_CONTENT_REASON: dict[str, GeminiContentRejectionReason] = {
    "SAFETY": GeminiContentRejectionReason.SAFETY,
    "RECITATION": GeminiContentRejectionReason.RECITATION,
    "BLOCKLIST": GeminiContentRejectionReason.BLOCKLIST,
    "PROHIBITED_CONTENT": GeminiContentRejectionReason.PROHIBITED_CONTENT,
    "SPII": GeminiContentRejectionReason.SPII,
}

# 写像の key から作り、片方だけが更新されてずれることを防ぐ。
OUTPUT_BLOCKED_FINISH_REASONS: frozenset[str] = frozenset(
    _FINISH_REASON_TO_CONTENT_REASON
)


_STATUS_TO_CONFIG_REASON: dict[str, GeminiStateReason] = {
    "UNAUTHENTICATED": GeminiStateReason.AUTH,
    "PERMISSION_DENIED": GeminiStateReason.PERMISSION_DENIED,
    "NOT_FOUND": GeminiStateReason.NOT_FOUND,
    "FAILED_PRECONDITION": GeminiStateReason.FAILED_PRECONDITION,
}


# gRPC status を持たない応答用。
_HTTP_CODE_TO_CONFIG_REASON: dict[int, GeminiStateReason] = {
    401: GeminiStateReason.AUTH,
    403: GeminiStateReason.PERMISSION_DENIED,
    404: GeminiStateReason.NOT_FOUND,
}


def output_blocked_reason(finish_reason_name: str) -> GeminiContentRejectionReason:
    """OUTPUT_BLOCKED_FINISH_REASONS の名前を拒否の理由に写す。無い名前は KeyError。"""
    return _FINISH_REASON_TO_CONTENT_REASON[finish_reason_name]


# 入力長の超過を示す文言 (Gemini の実際の応答から集めたもの)。
_CONTEXT_LENGTH_PATTERNS: tuple[str, ...] = (
    "exceeds context length",
    "context_length_exceeded",
    "exceeds the maximum number of tokens",
    "exceeds the maximum input token",
    "exceeds the model's context length",
    "exceeds the model's maximum context length",
    "input is too long",
)


def is_context_length_error(exc: Exception) -> bool:
    """入力長の超過を示す例外か。無関係な status の文言との偶然の一致は除く。"""
    if not isinstance(exc, genai_errors.APIError):
        return False
    status = getattr(exc, "status", None) or ""
    if status not in ("INVALID_ARGUMENT", "DEADLINE_EXCEEDED"):
        return False
    message = (getattr(exc, "message", None) or str(exc) or "").lower()
    return any(pat in message for pat in _CONTEXT_LENGTH_PATTERNS)


_QUOTA_FAILURE_TYPE = "type.googleapis.com/google.rpc.QuotaFailure"


def _has_per_day_quota_violation(exc: genai_errors.APIError) -> bool:
    """構造化 details に、日あたりの利用枠の超過があるか。

    429 は分あたりの超過でも日あたりの枯渇でも文言が同じため、details で判定する。
    details は ``{"error": {...}}`` と平らな形の両方がありうる。判定できなければ
    False (流量制限) に倒す。details は project ID などを含みうるのでログに出さない。
    """
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return False
    error = details.get("error", details)
    if not isinstance(error, dict):
        return False
    error_details = error.get("details")
    if not isinstance(error_details, list):
        return False
    for item in error_details:
        if not isinstance(item, dict) or item.get("@type") != _QUOTA_FAILURE_TYPE:
            continue
        violations = item.get("violations")
        if not isinstance(violations, list):
            continue
        for violation in violations:
            if not isinstance(violation, dict):
                continue
            quota_id = violation.get("quotaId")
            if isinstance(quota_id, str) and "perday" in quota_id.lower():
                return True
    return False


def translate_gemini_error(exc: Exception) -> Exception:
    """Gemini SDK の例外を分類し、分類できなければ元の例外を返す。

    SDK の生の message は PII を含みうるので、固定の文言だけを使う。設定系の status は
    INVALID_ARGUMENT の判定より先に見る (code=400 と FAILED_PRECONDITION が同居する
    応答では、SDK の版で揺れない status を優先する)。
    """
    if isinstance(exc, HostBlockedError):
        return AIProviderNetworkError(
            "AIプロバイダーへの通信が宛先の方針で拒否されました",
            reason=GeminiStateReason.HOST_BLOCKED,
        )
    failure = classify_httpx(exc)
    if failure is not None:
        if failure.reason is HttpTransportFailureReason.TIMEOUT:
            return AIProviderNetworkError(
                "AIプロバイダーとの通信がタイムアウトしました",
                reason=GeminiStateReason.TIMEOUT,
            )
        return AIProviderNetworkError(
            "AIプロバイダーに接続できませんでした", reason=GeminiStateReason.CONNECTION
        )
    if isinstance(exc, TimeoutError):
        return AIProviderNetworkError(
            "AIプロバイダーとの通信がタイムアウトしました",
            reason=GeminiStateReason.TIMEOUT,
        )
    if isinstance(exc, ConnectionError | OSError):
        return AIProviderNetworkError(
            "AIプロバイダーに接続できませんでした", reason=GeminiStateReason.CONNECTION
        )

    # ServerError は APIError の子クラスなので先に判定する。
    if isinstance(exc, genai_errors.ServerError):
        return AIProviderServiceUnavailableError(
            "AIプロバイダー内部でサーバーエラーが発生しました",
            reason=GeminiStateReason.SERVER_ERROR,
        )

    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        status = getattr(exc, "status", None) or ""
        raw_message = str(getattr(exc, "message", "")) or str(exc)
        message = raw_message.lower()

        if "reported as leaked" in message:
            return AIProviderConfigurationError(
                "AIプロバイダーがAPIキーの漏洩を検知しました",
                reason=GeminiStateReason.LEAKED_API_KEY,
            )

        if status in _STATUS_TO_CONFIG_REASON:
            reason = _STATUS_TO_CONFIG_REASON[status]
            return AIProviderConfigurationError(
                _CONFIG_REASON_MESSAGES[reason], reason=reason
            )

        if code in _HTTP_CODE_TO_CONFIG_REASON:
            reason = _HTTP_CODE_TO_CONFIG_REASON[code]
            return AIProviderConfigurationError(
                _CONFIG_REASON_MESSAGES[reason], reason=reason
            )

        if code == 400 or status == "INVALID_ARGUMENT":
            if "api key" in message:
                return AIProviderConfigurationError(
                    "AIプロバイダーの認証に失敗しました", reason=GeminiStateReason.AUTH
                )
            if "permission" in message:
                return AIProviderConfigurationError(
                    "AIプロバイダーへのアクセス権限がありません",
                    reason=GeminiStateReason.PERMISSION_DENIED,
                )
            if "blocked" in message or "safety" in message:
                return AIProviderInputRejectedError(
                    "AIプロバイダーが安全性の制約により入力を拒否しました",
                    reason=GeminiContentRejectionReason.INPUT_BLOCKED,
                )
            return AIProviderRequestInvalidError(
                "AIプロバイダーがリクエストの引数を不正と判定しました",
                reason=GeminiStateReason.INVALID_ARGUMENT,
            )

        # 日あたりの枯渇を確認できたときだけ枯渇とし、枯渇アラームの誤発火を避ける。
        if code == 429 or status == "RESOURCE_EXHAUSTED":
            if _has_per_day_quota_violation(exc):
                return AIProviderUsageLimitExhaustedError(
                    "AIプロバイダーの1日当たりの利用枠を使い切りました",
                    reason=GeminiStateReason.QUOTA_EXHAUSTED,
                )
            return AIProviderRateLimitedError(
                "AIプロバイダーの呼び出し頻度の上限に達しました",
                reason=GeminiStateReason.RATE_LIMITED,
            )

    return exc
