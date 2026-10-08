"""Gemini の失敗応答の details から、google.rpc の詳細を取り出す。

変換器の分類とログの変換が同じ読み方を使う。値は型だけを確かめ、記録してよい形かは
使う側が決める。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from google.genai import errors as genai_errors

_ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"
_QUOTA_FAILURE_TYPE = "type.googleapis.com/google.rpc.QuotaFailure"
_RETRY_INFO_TYPE = "type.googleapis.com/google.rpc.RetryInfo"


@dataclass(frozen=True, slots=True)
class QuotaViolation:
    """QuotaFailure の違反1件のうち、診断に使う値。"""

    quota_id: str | None
    quota_value: str | None


@dataclass(frozen=True, slots=True)
class ResponseErrorDetails:
    """失敗応答の details から取り出した、診断に使う値。"""

    error_info_reason: str | None
    error_info_domain: str | None
    quota_violations: tuple[QuotaViolation, ...]
    retry_delay: str | None


def read_response_error_details(
    exc: genai_errors.APIError,
) -> ResponseErrorDetails:
    """ErrorInfo・QuotaFailure・RetryInfo を取り出し、型が合わない値は None にする。"""
    items = _detail_items(exc)
    error_info = _first_item(items, _ERROR_INFO_TYPE)
    retry_info = _first_item(items, _RETRY_INFO_TYPE)
    return ResponseErrorDetails(
        error_info_reason=_string(error_info, "reason"),
        error_info_domain=_string(error_info, "domain"),
        quota_violations=_quota_violations(items),
        retry_delay=_string(retry_info, "retryDelay"),
    )


def _detail_items(exc: genai_errors.APIError) -> list[dict[str, Any]]:
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


def _first_item(items: list[dict[str, Any]], type_url: str) -> dict[str, Any] | None:
    return next((item for item in items if item.get("@type") == type_url), None)


def _string(item: dict[str, Any] | None, key: str) -> str | None:
    value = item.get(key) if item is not None else None
    return value if isinstance(value, str) else None


def _quota_violations(items: list[dict[str, Any]]) -> tuple[QuotaViolation, ...]:
    violations: list[QuotaViolation] = []
    for item in items:
        if item.get("@type") != _QUOTA_FAILURE_TYPE:
            continue
        entries = item.get("violations")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                violations.append(
                    QuotaViolation(
                        quota_id=_string(entry, "quotaId"),
                        quota_value=_string(entry, "quotaValue"),
                    )
                )
    return tuple(violations)
