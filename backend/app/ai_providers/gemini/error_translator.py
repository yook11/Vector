"""Gemini SDK の例外を AIProvider*Error に分類する。

工程によらず同じ障害を同じ例外にするため、SDK の例外を見る分類はここに集める。
finish_reason や応答の形の検証は、各工程の adapter が持つ。
"""

from __future__ import annotations

from google.genai import errors as genai_errors

from app.ai_providers.errors import (
    AIProviderErrorResponseError,
    AIProviderErrorResponseReason,
    AIProviderGenerationReason,
    AIProviderRequestNotSentError,
    AIProviderRequestNotSentReason,
    AIProviderTransportError,
)
from app.http.destination_policy import HostBlockedError
from app.http.failure import classify_httpx

_CONFIG_REASON_MESSAGES: dict[AIProviderErrorResponseReason, str] = {
    AIProviderErrorResponseReason.AUTH: "AIプロバイダーの認証に失敗しました",
    AIProviderErrorResponseReason.PERMISSION_DENIED: (
        "AIプロバイダーへのアクセス権限がありません"
    ),
    AIProviderErrorResponseReason.NOT_FOUND: "AIプロバイダーの要求先が見つかりません",
    AIProviderErrorResponseReason.FAILED_PRECONDITION: (
        "AIプロバイダーの利用に必要な前提条件が満たされていません"
    ),
}


_FINISH_REASON_TO_GENERATION_REASON: dict[str, AIProviderGenerationReason] = {
    "SAFETY": AIProviderGenerationReason.OUTPUT_BLOCKED_SAFETY,
    "RECITATION": AIProviderGenerationReason.OUTPUT_BLOCKED_RECITATION,
    "BLOCKLIST": AIProviderGenerationReason.OUTPUT_BLOCKED_BLOCKLIST,
    "PROHIBITED_CONTENT": AIProviderGenerationReason.OUTPUT_BLOCKED_PROHIBITED_CONTENT,
    "SPII": AIProviderGenerationReason.OUTPUT_BLOCKED_SPII,
}

# 写像の key から作り、片方だけが更新されてずれることを防ぐ。
OUTPUT_BLOCKED_FINISH_REASONS: frozenset[str] = frozenset(
    _FINISH_REASON_TO_GENERATION_REASON
)


_STATUS_TO_CONFIG_REASON: dict[str, AIProviderErrorResponseReason] = {
    "UNAUTHENTICATED": AIProviderErrorResponseReason.AUTH,
    "PERMISSION_DENIED": AIProviderErrorResponseReason.PERMISSION_DENIED,
    "NOT_FOUND": AIProviderErrorResponseReason.NOT_FOUND,
    "FAILED_PRECONDITION": AIProviderErrorResponseReason.FAILED_PRECONDITION,
}


# gRPC status を持たない応答用。
_HTTP_CODE_TO_CONFIG_REASON: dict[int, AIProviderErrorResponseReason] = {
    401: AIProviderErrorResponseReason.AUTH,
    403: AIProviderErrorResponseReason.PERMISSION_DENIED,
    404: AIProviderErrorResponseReason.NOT_FOUND,
}


def output_blocked_reason(finish_reason_name: str) -> AIProviderGenerationReason:
    """OUTPUT_BLOCKED_FINISH_REASONS の名前を拒否の理由に写す。無い名前は KeyError。"""
    return _FINISH_REASON_TO_GENERATION_REASON[finish_reason_name]


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


def _is_context_length_error(status: str, message: str) -> bool:
    """入力長の超過を示すか。無関係な status の文言との偶然の一致は除く。"""
    if status not in ("INVALID_ARGUMENT", "DEADLINE_EXCEEDED"):
        return False
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
        return AIProviderRequestNotSentError(
            "AIプロバイダーへの通信が宛先の方針で拒否されました",
            reason=AIProviderRequestNotSentReason.HOST_BLOCKED,
        )
    transport = classify_httpx(exc)
    if transport is not None:
        return AIProviderTransportError(transport=transport)
    if not isinstance(exc, genai_errors.APIError):
        return exc

    status_code = exc.code
    status = exc.status or ""
    message = (exc.message or str(exc)).lower()

    # 入力長の超過は DEADLINE_EXCEEDED (5xx) でも返るので、ServerError より先に見る。
    if _is_context_length_error(status, message):
        return AIProviderErrorResponseError(
            "AIプロバイダーが入力長の上限を超えたと判定しました",
            reason=AIProviderErrorResponseReason.INPUT_TOO_LONG,
            status_code=status_code,
        )

    # ServerError は APIError の子クラスなので先に判定する。
    if isinstance(exc, genai_errors.ServerError):
        return AIProviderErrorResponseError(
            "AIプロバイダー内部でサーバーエラーが発生しました",
            reason=AIProviderErrorResponseReason.SERVER_ERROR,
            status_code=status_code,
        )

    if "reported as leaked" in message:
        return AIProviderErrorResponseError(
            "AIプロバイダーがAPIキーの漏洩を検知しました",
            reason=AIProviderErrorResponseReason.LEAKED_API_KEY,
            status_code=status_code,
        )

    config_reason = _STATUS_TO_CONFIG_REASON.get(
        status
    ) or _HTTP_CODE_TO_CONFIG_REASON.get(status_code)
    if config_reason is not None:
        return AIProviderErrorResponseError(
            _CONFIG_REASON_MESSAGES[config_reason],
            reason=config_reason,
            status_code=status_code,
        )

    if status_code == 400 or status == "INVALID_ARGUMENT":
        if "api key" in message:
            return AIProviderErrorResponseError(
                "AIプロバイダーの認証に失敗しました",
                reason=AIProviderErrorResponseReason.AUTH,
                status_code=status_code,
            )
        if "permission" in message:
            return AIProviderErrorResponseError(
                "AIプロバイダーへのアクセス権限がありません",
                reason=AIProviderErrorResponseReason.PERMISSION_DENIED,
                status_code=status_code,
            )
        if "blocked" in message or "safety" in message:
            return AIProviderErrorResponseError(
                "AIプロバイダーが安全性の制約により入力を拒否しました",
                reason=AIProviderErrorResponseReason.INPUT_BLOCKED,
                status_code=status_code,
            )
        return AIProviderErrorResponseError(
            "AIプロバイダーがリクエストの引数を不正と判定しました",
            reason=AIProviderErrorResponseReason.INVALID_REQUEST,
            status_code=status_code,
        )

    # 日あたりの枯渇を確認できたときだけ枯渇とし、枯渇アラームの誤発火を避ける。
    if status_code == 429 or status == "RESOURCE_EXHAUSTED":
        if _has_per_day_quota_violation(exc):
            return AIProviderErrorResponseError(
                "AIプロバイダーの1日当たりの利用枠を使い切りました",
                reason=AIProviderErrorResponseReason.QUOTA_EXHAUSTED,
                status_code=status_code,
            )
        return AIProviderErrorResponseError(
            "AIプロバイダーの呼び出し頻度の上限に達しました",
            reason=AIProviderErrorResponseReason.RATE_LIMITED,
            status_code=status_code,
        )

    return exc
