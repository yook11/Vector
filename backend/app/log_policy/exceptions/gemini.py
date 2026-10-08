"""Gemini SDK 例外から、応答の自由文を含まない原因情報を取り出す。"""

from __future__ import annotations

import re
from typing import Literal, NotRequired, TypedDict, TypeGuard

from google.genai import errors as genai_errors

from app.ai_providers.gemini.error_details import read_response_error_details
from app.log_policy.exceptions.types import ConvertedException

# AI分析ログ仕様の識別子（provider_code）の規則。値の一覧では絞らない。
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
# ProtoJSON の int64（数字の文字列）と Duration（小数部は0・3・6・9桁）の形。
_QUOTA_VALUE = re.compile(r"[0-9]{1,19}")
_RETRY_DELAY = re.compile(r"([0-9]{1,6})(\.[0-9]{1,9})?s")
# 説明文のうち、入力長の超過として公開されている形だけから数値を取る。
_INPUT_TOKEN_COUNTS = re.compile(
    r"The input token count \(([0-9]{1,12})\) exceeds the maximum number"
    r" of tokens allowed \(([0-9]{1,12})\)"
)


class GeminiErrorInfo(TypedDict, total=False):
    """ErrorInfo のうち、識別子として残す値。"""

    reason: str
    domain: str


class GeminiQuotaViolation(TypedDict, total=False):
    """QuotaFailure の違反1件のうち、残す値。"""

    quota_id: str
    quota_value: int


class GeminiErrorDetails(TypedDict):
    """原因文と独立して保持する Gemini API の診断属性。"""

    kind: Literal["gemini"]
    http_status: NotRequired[int]
    provider_code: NotRequired[str]
    error_info: NotRequired[GeminiErrorInfo]
    quota_violations: NotRequired[list[GeminiQuotaViolation]]
    retry_delay_seconds: NotRequired[int | float]
    input_tokens: NotRequired[int]
    max_input_tokens: NotRequired[int]


def convert_gemini_exception(
    exc: genai_errors.APIError | genai_errors.UnknownApiResponseError,
) -> ConvertedException:
    """応答の説明文を使わず、固定の文と形を確かめた診断を共通形式へ渡す。"""
    # 説明文に生の応答を含むため、固定の文だけにする。
    if isinstance(exc, genai_errors.UnknownApiResponseError):
        return ConvertedException(
            message="Gemini API response could not be parsed as JSON"
        )
    try:
        details = extract_gemini_error_details(exc)
        message = "Gemini API error"
        provider_code = details.get("provider_code")
        if provider_code is not None:
            message += f": {provider_code}"
            reason = details.get("error_info", {}).get("reason")
            if reason is not None:
                message += f" / {reason}"
        return ConvertedException(
            message=message, error_details=details if len(details) > 1 else None
        )
    except Exception:
        return ConvertedException(message="[exception message unavailable]")


def extract_gemini_error_details(exc: genai_errors.APIError) -> GeminiErrorDetails:
    """取得できた値のうち、形が合うものだけを診断へ入れる。"""
    details: GeminiErrorDetails = {"kind": "gemini"}

    # HTTP の数字は実際の応答から取り、本文の数字（exc.code）で補わない。
    # ストリームの中の失敗では、SDK が自身の型で応答を包むので型では見分けない。
    status_code = getattr(exc.response, "status_code", None)
    if type(status_code) is int and 100 <= status_code <= 599:
        details["http_status"] = status_code
    if _is_identifier(exc.status):
        details["provider_code"] = exc.status

    try:
        response_details = read_response_error_details(exc)

        error_info: GeminiErrorInfo = {}
        if _is_identifier(response_details.error_info_reason):
            error_info["reason"] = response_details.error_info_reason
        if _is_identifier(response_details.error_info_domain):
            error_info["domain"] = response_details.error_info_domain
        if error_info:
            details["error_info"] = error_info

        violations: list[GeminiQuotaViolation] = []
        for violation in response_details.quota_violations:
            entry: GeminiQuotaViolation = {}
            if _is_identifier(violation.quota_id):
                entry["quota_id"] = violation.quota_id
            if violation.quota_value is not None and _QUOTA_VALUE.fullmatch(
                violation.quota_value
            ):
                entry["quota_value"] = int(violation.quota_value)
            if entry:
                violations.append(entry)
        if violations:
            details["quota_violations"] = violations

        retry_delay = response_details.retry_delay
        delay = _RETRY_DELAY.fullmatch(retry_delay) if retry_delay else None
        if delay is not None:
            details["retry_delay_seconds"] = (
                float(retry_delay[:-1]) if delay[2] else int(delay[1])
            )

        token_counts = (
            _INPUT_TOKEN_COUNTS.search(exc.message)
            if isinstance(exc.message, str)
            else None
        )
        if token_counts is not None:
            details["input_tokens"] = int(token_counts[1])
            details["max_input_tokens"] = int(token_counts[2])
    except Exception:  # noqa: S110
        # 応答の詳細が壊れていても、HTTP の数字と status は残す。
        pass
    return details


def _is_identifier(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None
