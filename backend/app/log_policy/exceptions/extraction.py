"""例外の原因とグループを上限内で探索し、変換結果をログフィールドへまとめる。

秘密情報のサニタイズとマスクは値準備が担当する。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from types import TracebackType
from typing import Literal, NotRequired, TypedDict

from app.log_policy.exceptions.conversion import (
    ErrorDetails,
    ExceptionConverter,
    convert_exception,
)

FRAME_LIMIT = 50
EXCEPTION_LIMIT = 32
CAUSE_DEPTH_LIMIT = 8

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
    error_details: NotRequired[ErrorDetails]
    causes: NotRequired[list[ExceptionLogFields] | Literal["[limit]", "[cycle]"]]
    exceptions: NotRequired[list[ExceptionLogFields | Literal["[limit]", "[cycle]"]]]


@dataclass
class _ExceptionBudget:
    """原因とグループの全枝で、読み取れる例外の残数を共有する。"""

    remaining: int


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


def extract_exception_fields(
    exc_info: object,
    *,
    exception_converter: ExceptionConverter = convert_exception,
) -> ExceptionLogFields | None:
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

    return _extract_exception_node(
        exc,
        tb,
        depth=0,
        ancestors=set(),
        budget=_ExceptionBudget(remaining=EXCEPTION_LIMIT - 1),
        exception_converter=exception_converter,
    )


def _extract_exception_child(
    exc: BaseException,
    *,
    depth: int,
    ancestors: set[int],
    budget: _ExceptionBudget,
    exception_converter: ExceptionConverter,
) -> ExceptionLogFields | Literal["[limit]", "[cycle]"]:
    """子の属性や型を調べる前に、探索の上限を確認する。"""
    if depth > CAUSE_DEPTH_LIMIT or budget.remaining <= 0:
        return "[limit]"

    # 循環参照の確認も数え、同じ参照が並ぶグループの走査を制限する。
    budget.remaining -= 1
    if id(exc) in ancestors:
        return "[cycle]"

    return _extract_exception_node(
        exc,
        exc.__traceback__,
        depth=depth,
        ancestors=ancestors,
        budget=budget,
        exception_converter=exception_converter,
    )


def _extract_exception_node(
    exc: BaseException,
    tb: TracebackType | None,
    *,
    depth: int,
    ancestors: set[int],
    budget: _ExceptionBudget,
    exception_converter: ExceptionConverter,
) -> ExceptionLogFields:
    """例外1件の変換結果と発生位置をまとめ、未集約の原因とメンバーを辿る。"""
    ancestors = ancestors | {id(exc)}
    converted = exception_converter(exc)

    fields: ExceptionLogFields = {
        "error_class": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "error_message": converted.message,
        "frames": extract_exception_frames(tb),
    }
    if converted.error_details is not None:
        fields["error_details"] = converted.error_details
    if converted.cause_is_aggregated:
        return fields

    cause = exc.__cause__
    if cause is None and not exc.__suppress_context__:
        cause = exc.__context__
    if cause is not None:
        child = _extract_exception_child(
            cause,
            depth=depth + 1,
            ancestors=ancestors,
            budget=budget,
            exception_converter=exception_converter,
        )
        fields["causes"] = [child] if isinstance(child, dict) else child

    if isinstance(exc, BaseExceptionGroup):
        children: list[ExceptionLogFields | Literal["[limit]", "[cycle]"]] = []
        for member in exc.exceptions:
            child = _extract_exception_child(
                member,
                depth=depth + 1,
                ancestors=ancestors,
                budget=budget,
                exception_converter=exception_converter,
            )
            children.append(child)
            if child == "[limit]":
                break
        fields["exceptions"] = children
    return fields
