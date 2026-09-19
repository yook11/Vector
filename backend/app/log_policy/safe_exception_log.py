"""例外の種類に固有の入力値を除いてログフィールドへ変換する。

秘密情報のサニタイズとマスクは値準備が担当する。
"""

from __future__ import annotations

import sys
from types import TracebackType
from typing import Literal, NotRequired, TypedDict

from pydantic import ValidationError
from sqlalchemy.exc import StatementError

from app.log_policy.exceptions.sql import extract_sql_error_message, extract_sqlstate
from app.log_policy.exceptions.validation import extract_validation_message

FRAME_LIMIT = 50

# structlog / logging と同じ3形式。揃えた先は sys.exc_info() と同じ並び。
type ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]
type ExcInfoInput = Literal[True] | BaseException | ExcInfo


class ExceptionLogFrame(TypedDict):
    """値やソース行を含まない例外発生位置。"""

    file: str
    function: str
    line: int


class ExceptionLogFields(TypedDict):
    """例外から抽出し、共通の検査・サニタイズへ渡すフィールド。"""

    error_class: str
    error_message: str
    frames: list[ExceptionLogFrame] | Literal["[limit]"]
    sqlstate: NotRequired[str]


def extract_exception_frames(
    tb: TracebackType | None,
) -> list[ExceptionLogFrame] | Literal["[limit]"]:
    """値を含まないframeを抽出し、件数超過時は部分リストを残さない。"""
    frames: list[ExceptionLogFrame] = []
    while tb is not None:
        if len(frames) >= FRAME_LIMIT:
            return "[limit]"
        code = tb.tb_frame.f_code
        frames.append(
            {
                "file": code.co_filename,
                "function": code.co_name,
                "line": tb.tb_lineno,
            }
        )
        tb = tb.tb_next
    return frames


def extract_exception_fields(exc_info: object) -> ExceptionLogFields | None:
    """ExcInfoInput に合う値だけを揃え、サニタイズ前の例外フィールドを返す。"""
    # 3形式を sys.exc_info() と同じ並びへ揃え、合わない値はフィールドを作らない。
    if exc_info is True:
        exc_info = sys.exc_info()
    if isinstance(exc_info, BaseException):
        exc_info = type(exc_info), exc_info, exc_info.__traceback__
    if type(exc_info) is not tuple or len(exc_info) != 3:
        return None
    exc_type, exc, tb = exc_info
    if not isinstance(exc, BaseException) or exc_type is not type(exc):
        return None
    if tb is not None and not isinstance(tb, TracebackType):
        return None

    # 原因文は例外の種類ごとに入力値を除いて取り、取得に失敗したら固定文にする。
    sqlstate: str | None = None
    try:
        if isinstance(exc, StatementError):
            message = extract_sql_error_message(exc)
            sqlstate = extract_sqlstate(exc)
        elif isinstance(exc, ValidationError):
            message = extract_validation_message(exc)
        else:
            message = str(exc)
    except Exception:
        message = "[exception message unavailable]"

    fields: ExceptionLogFields = {
        "error_class": f"{exc_type.__module__}.{exc_type.__qualname__}",
        "error_message": message,
        "frames": extract_exception_frames(tb),
    }
    if sqlstate is not None:
        fields["sqlstate"] = sqlstate
    return fields
