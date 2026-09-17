"""例外から入力値と秘密情報を除外し、安全なログフィールドへ変換する。"""

from __future__ import annotations

import sys
from collections import deque
from types import TracebackType
from typing import Any, NotRequired, TypedDict

from app.log_policy.exception_messages import (
    extract_exception_message,
    extract_sqlstate,
)
from app.log_policy.rules import BASE_DENY
from app.log_policy.sanitize import TEXT_LIMIT, sanitize_text

FRAME_LIMIT = 10


class ExceptionLogFrame(TypedDict):
    """値やソース行を含まない例外発生位置。"""

    file: str
    function: str
    line: int


class ExceptionLogFields(TypedDict):
    """保護処理を通して生成する例外ログの出力形。"""

    error_class: str
    error_message: str
    frames: list[ExceptionLogFrame]
    sqlstate: NotRequired[str]


def _resolve_exc_info(
    value: object,
) -> tuple[type[BaseException], BaseException, TracebackType | None] | None:
    """不正な tuple を拒否してから例外実体を取り出す。"""
    if value is True:
        value = sys.exc_info()
    if isinstance(value, BaseException):
        return type(value), value, value.__traceback__
    if type(value) is tuple and len(value) == 3:
        exc_type, exc, tb = value
        if isinstance(exc, BaseException) and exc_type is type(exc):
            if tb is None or isinstance(tb, TracebackType):
                return exc_type, exc, tb
    return None


def extract_exception_frames(tb: TracebackType | None) -> list[ExceptionLogFrame]:
    """traceback から値を含まない発生位置を最大件数まで取り出す。"""
    frames: deque[ExceptionLogFrame] = deque(maxlen=FRAME_LIMIT)
    while tb is not None:
        code = tb.tb_frame.f_code
        frames.append(
            {
                "file": sanitize_text(code.co_filename)[:TEXT_LIMIT],
                "function": sanitize_text(code.co_name)[:TEXT_LIMIT],
                "line": tb.tb_lineno,
            }
        )
        tb = tb.tb_next
    return list(frames)


def build_exception_log_fields(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
    deny: frozenset[str],
) -> ExceptionLogFields:
    """検証済みの例外を安全なログフィールドへ変換する。"""
    extracted_message = extract_exception_message(exc)
    protected_message = sanitize_text(extracted_message, deny)
    result: ExceptionLogFields = {
        "error_class": sanitize_text(f"{exc_type.__module__}.{exc_type.__qualname__}")[
            :TEXT_LIMIT
        ],
        "error_message": protected_message[:TEXT_LIMIT],
        "frames": extract_exception_frames(tb),
    }
    try:
        sqlstate = extract_sqlstate(exc)
        if sqlstate is not None:
            result["sqlstate"] = sqlstate
    except Exception:
        result["error_message"] = "[exception metadata unavailable]"
    return result


def extract_safe_exception_fields(
    value: Any, *, deny: frozenset[str] = BASE_DENY
) -> ExceptionLogFields | None:
    """structlog の exc_info から安全な例外ログフィールドを抽出する。"""
    resolved = _resolve_exc_info(value)
    return (
        build_exception_log_fields(*resolved, deny=deny)
        if resolved is not None
        else None
    )
