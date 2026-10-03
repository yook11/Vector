"""Gemini SDK の例外を AIProvider*Error に分類する。

工程によらず同じ障害を同じ例外にするため、SDK の例外を見る分類はここに集める。
finish_reason や応答の形の検証は、各工程の adapter が持つ。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx2
import structlog
from google.genai import errors as genai_errors

from app.ai_providers.errors import (
    AIProviderNotSentError,
    AIProviderNotSentReason,
    AIProviderResponseError,
    AIProviderResponseReason,
    AIProviderResultReason,
    AIProviderTransportError,
)
from app.http.destination_policy import HostBlockedError
from app.http.error_mapping import (
    http_response_error_from_status,
    http_transport_error_from_exception,
)

logger = structlog.get_logger(__name__)

_CONFIG_REASON_MESSAGES: dict[AIProviderResponseReason, str] = {
    AIProviderResponseReason.AUTH: "AIプロバイダーの認証に失敗しました",
    AIProviderResponseReason.PERMISSION_DENIED: (
        "AIプロバイダーへのアクセス権限がありません"
    ),
    AIProviderResponseReason.NOT_FOUND: "AIプロバイダーの要求先が見つかりません",
    AIProviderResponseReason.FAILED_PRECONDITION: (
        "AIプロバイダーの利用に必要な前提条件が満たされていません"
    ),
}


_FINISH_REASON_TO_RESULT_REASON: dict[str, AIProviderResultReason] = {
    "SAFETY": AIProviderResultReason.OUTPUT_BLOCKED_SAFETY,
    "RECITATION": AIProviderResultReason.OUTPUT_BLOCKED_RECITATION,
    "BLOCKLIST": AIProviderResultReason.OUTPUT_BLOCKED_BLOCKLIST,
    "PROHIBITED_CONTENT": AIProviderResultReason.OUTPUT_BLOCKED_PROHIBITED_CONTENT,
    "SPII": AIProviderResultReason.OUTPUT_BLOCKED_SPII,
}

# 写像の key から作り、片方だけが更新されてずれることを防ぐ。
OUTPUT_BLOCKED_FINISH_REASONS: frozenset[str] = frozenset(
    _FINISH_REASON_TO_RESULT_REASON
)


_STATUS_TO_CONFIG_REASON: dict[str, AIProviderResponseReason] = {
    "UNAUTHENTICATED": AIProviderResponseReason.AUTH,
    "PERMISSION_DENIED": AIProviderResponseReason.PERMISSION_DENIED,
    "NOT_FOUND": AIProviderResponseReason.NOT_FOUND,
    "FAILED_PRECONDITION": AIProviderResponseReason.FAILED_PRECONDITION,
}


# gRPC status を持たない応答用。
_HTTP_CODE_TO_CONFIG_REASON: dict[int, AIProviderResponseReason] = {
    401: AIProviderResponseReason.AUTH,
    403: AIProviderResponseReason.PERMISSION_DENIED,
    404: AIProviderResponseReason.NOT_FOUND,
}


def output_blocked_reason(finish_reason_name: str) -> AIProviderResultReason:
    """OUTPUT_BLOCKED_FINISH_REASONS の名前を拒否の理由に写す。無い名前は KeyError。"""
    return _FINISH_REASON_TO_RESULT_REASON[finish_reason_name]


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
_RETRY_INFO_TYPE = "type.googleapis.com/google.rpc.RetryInfo"

# ログには、応答の任意の文字列を持ち込まないよう既知の形の値だけを出す。
_LOGGABLE_QUOTA_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_LOGGABLE_RETRY_DELAY = re.compile(r"[0-9]{1,6}(\.[0-9]{1,9})?s")


def _error_detail_items(exc: genai_errors.APIError) -> list[dict[str, Any]]:
    """本文の構造化 details の要素を取り出し、形が不正なら空にする。

    details は ``{"error": {...}}`` と平らな形の両方がありうる。project ID などを
    含みうるので、まるごとはログに出さない。
    """
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return []
    error = details.get("error", details)
    if not isinstance(error, dict):
        return []
    items = error.get("details")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _quota_ids(items: list[dict[str, Any]]) -> list[str]:
    """QuotaFailure の違反から quotaId を取り出す。"""
    quota_ids: list[str] = []
    for item in items:
        if item.get("@type") != _QUOTA_FAILURE_TYPE:
            continue
        violations = item.get("violations")
        if not isinstance(violations, list):
            continue
        for violation in violations:
            if isinstance(violation, dict):
                quota_id = violation.get("quotaId")
                if isinstance(quota_id, str):
                    quota_ids.append(quota_id)
    return quota_ids


def _retry_delay(items: list[dict[str, Any]]) -> str | None:
    """RetryInfo の再試行までの待ち時間 (例 ``"53s"``) を取り出す。"""
    for item in items:
        if item.get("@type") != _RETRY_INFO_TYPE:
            continue
        retry_delay = item.get("retryDelay")
        if isinstance(retry_delay, str):
            return retry_delay
    return None


def _has_per_day_quota_violation(quota_ids: list[str]) -> bool:
    """日あたりの利用枠の超過があるか。

    429 は分あたりの超過でも日あたりの枯渇でも文言が同じため、details の quotaId で
    判定する。判定できなければ False (流量制限) に倒す。
    """
    return any("perday" in quota_id.lower() for quota_id in quota_ids)


def _record_resource_exhausted(
    reason: AIProviderResponseReason,
    *,
    items: list[dict[str, Any]],
    quota_ids: list[str],
    retry_after_header: bool,
) -> None:
    """再試行の時刻を決める材料として、429 の待ち時間と枠の種類を記録する。"""
    retry_delay = _retry_delay(items)
    try:
        logger.warning(
            "gemini_resource_exhausted",
            reason=reason.value,
            retry_delay=(
                retry_delay
                if retry_delay is not None
                and _LOGGABLE_RETRY_DELAY.fullmatch(retry_delay)
                else None
            ),
            quota_ids=[
                quota_id
                for quota_id in quota_ids
                if _LOGGABLE_QUOTA_ID.fullmatch(quota_id)
            ],
            retry_after_header=retry_after_header,
        )
    except Exception:  # noqa: S110
        # 観測ログの障害で分類の結果を変えない。
        pass


def translate_gemini_error(exc: Exception) -> Exception:
    """Gemini SDK の例外を分類し、分類できなければ元の例外を返す。

    SDK の生の message は PII を含みうるので、固定の文言だけを使う。設定系の status は
    INVALID_ARGUMENT の判定より先に見る (code=400 と FAILED_PRECONDITION が同居する
    応答では、SDK の版で揺れない status を優先する)。
    """
    if isinstance(exc, HostBlockedError):
        return AIProviderNotSentError(
            "AIプロバイダーへの通信が宛先の方針で拒否されました",
            reason=AIProviderNotSentReason.HOST_BLOCKED,
        )
    transport_error = http_transport_error_from_exception(exc)
    if transport_error is not None:
        return AIProviderTransportError(http_error=transport_error)
    if not isinstance(exc, genai_errors.APIError):
        return exc

    status_code = exc.code
    # 変換器は SDK の例外を捕まえた直後に呼ばれるので、今の時刻を受信時刻とする。
    http_error = http_response_error_from_status(
        status_code,
        response=exc.response if isinstance(exc.response, httpx2.Response) else None,
        received_at=datetime.now(UTC),
    )
    status = exc.status or ""
    message = (exc.message or str(exc)).lower()

    # 入力長の超過は DEADLINE_EXCEEDED (5xx) でも返るので、ServerError より先に見る。
    if _is_context_length_error(status, message):
        return AIProviderResponseError(
            "AIプロバイダーが入力長の上限を超えたと判定しました",
            reason=AIProviderResponseReason.INPUT_TOO_LONG,
            http_error=http_error,
        )

    # ServerError は APIError の子クラスなので先に判定する。
    if isinstance(exc, genai_errors.ServerError):
        return AIProviderResponseError(
            "AIプロバイダー内部でサーバーエラーが発生しました",
            reason=AIProviderResponseReason.SERVER_ERROR,
            http_error=http_error,
        )

    if "reported as leaked" in message:
        return AIProviderResponseError(
            "AIプロバイダーがAPIキーの漏洩を検知しました",
            reason=AIProviderResponseReason.LEAKED_API_KEY,
            http_error=http_error,
        )

    config_reason = _STATUS_TO_CONFIG_REASON.get(
        status
    ) or _HTTP_CODE_TO_CONFIG_REASON.get(status_code)
    if config_reason is not None:
        return AIProviderResponseError(
            _CONFIG_REASON_MESSAGES[config_reason],
            reason=config_reason,
            http_error=http_error,
        )

    if status_code == 400 or status == "INVALID_ARGUMENT":
        if "api key" in message:
            return AIProviderResponseError(
                "AIプロバイダーの認証に失敗しました",
                reason=AIProviderResponseReason.AUTH,
                http_error=http_error,
            )
        if "permission" in message:
            return AIProviderResponseError(
                "AIプロバイダーへのアクセス権限がありません",
                reason=AIProviderResponseReason.PERMISSION_DENIED,
                http_error=http_error,
            )
        if "blocked" in message or "safety" in message:
            return AIProviderResponseError(
                "AIプロバイダーが安全性の制約により入力を拒否しました",
                reason=AIProviderResponseReason.INPUT_BLOCKED,
                http_error=http_error,
            )
        return AIProviderResponseError(
            "AIプロバイダーがリクエストの引数を不正と判定しました",
            reason=AIProviderResponseReason.INVALID_REQUEST,
            http_error=http_error,
        )

    # 日あたりの枯渇を確認できたときだけ枯渇とし、枯渇アラームの誤発火を避ける。
    if status_code == 429 or status == "RESOURCE_EXHAUSTED":
        items = _error_detail_items(exc)
        quota_ids = _quota_ids(items)
        if _has_per_day_quota_violation(quota_ids):
            translated = AIProviderResponseError(
                "AIプロバイダーの1日当たりの利用枠を使い切りました",
                reason=AIProviderResponseReason.QUOTA_EXHAUSTED,
                http_error=http_error,
            )
        else:
            translated = AIProviderResponseError(
                "AIプロバイダーの呼び出し頻度の上限に達しました",
                reason=AIProviderResponseReason.RATE_LIMITED,
                http_error=http_error,
            )
        _record_resource_exhausted(
            translated.reason,
            items=items,
            quota_ids=quota_ids,
            retry_after_header=http_error.retry_after is not None,
        )
        return translated

    return exc
