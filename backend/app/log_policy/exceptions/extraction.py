"""例外の原因とグループを上限内で探索し、変換結果をログフィールドへまとめる。

秘密情報のサニタイズとマスクは値準備が担当する。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, NotRequired, TypedDict

from app.log_policy.exceptions.conversion import convert_exception
from app.log_policy.exceptions.types import ErrorDetails, ExceptionConverter

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


class LoggedException(TypedDict):
    """例外1件の型・原因文・発生位置と、変換担当が作った診断情報。"""

    error_class: str
    error_message: str
    frames: list[ExceptionLogFrame] | Literal["[limit]"]
    error_details: NotRequired[ErrorDetails]


type ExceptionRelation = Literal["cause", "context", "member"]


class RelatedException(TypedDict):
    """関連する例外1件と、関係元の位置と関係の種類。"""

    # 関係元が外側の例外なら None、関連する例外なら並びの中の位置。
    parent: int | None
    relation: ExceptionRelation
    exception: LoggedException | Literal["[limit]", "[cycle]"]


class ExceptionLogFields(LoggedException):
    """例外から抽出し、共通の検査・サニタイズへ渡すフィールド。"""

    related_exceptions: NotRequired[list[RelatedException]]


@dataclass
class _RelatedExceptionTraversal:
    """関連する例外を深さ優先で並べ、全枝で読み取れる例外の残数を共有する。"""

    exception_converter: ExceptionConverter
    remaining: int
    related: list[RelatedException] = field(default_factory=list)

    def follow(
        self,
        exc: BaseException,
        *,
        position: int | None,
        depth: int,
        ancestors: set[int],
    ) -> None:
        """前の例外を先に、グループのメンバーを元の順序で後に並べる。"""
        if exc.__cause__ is not None:
            self.append(
                exc.__cause__,
                "cause",
                parent=position,
                depth=depth + 1,
                ancestors=ancestors,
            )
        elif exc.__context__ is not None and not exc.__suppress_context__:
            self.append(
                exc.__context__,
                "context",
                parent=position,
                depth=depth + 1,
                ancestors=ancestors,
            )

        if isinstance(exc, BaseExceptionGroup):
            for member in exc.exceptions:
                omitted = self.append(
                    member,
                    "member",
                    parent=position,
                    depth=depth + 1,
                    ancestors=ancestors,
                )
                if omitted:
                    break

    def append(
        self,
        exc: BaseException,
        relation: ExceptionRelation,
        *,
        parent: int | None,
        depth: int,
        ancestors: set[int],
    ) -> bool:
        """子の属性や型を調べる前に上限を確認して1件並べ、上限で省略したかを返す。"""
        if depth > CAUSE_DEPTH_LIMIT or self.remaining <= 0:
            self.related.append(
                {"parent": parent, "relation": relation, "exception": "[limit]"}
            )
            return True

        # 循環参照の確認も数え、同じ参照が並ぶグループの走査を制限する。
        self.remaining -= 1
        if id(exc) in ancestors:
            self.related.append(
                {"parent": parent, "relation": relation, "exception": "[cycle]"}
            )
            return False

        logged, cause_is_aggregated = _describe_exception(
            exc, exc.__traceback__, self.exception_converter
        )
        position = len(self.related)
        self.related.append(
            {"parent": parent, "relation": relation, "exception": logged}
        )
        if not cause_is_aggregated:
            self.follow(
                exc,
                position=position,
                depth=depth,
                ancestors=ancestors | {id(exc)},
            )
        return False


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

    logged, cause_is_aggregated = _describe_exception(exc, tb, exception_converter)
    fields: ExceptionLogFields = {**logged}
    if cause_is_aggregated:
        return fields

    traversal = _RelatedExceptionTraversal(
        exception_converter=exception_converter,
        remaining=EXCEPTION_LIMIT - 1,
    )
    traversal.follow(exc, position=None, depth=0, ancestors={id(exc)})
    if traversal.related:
        fields["related_exceptions"] = traversal.related
    return fields


def _describe_exception(
    exc: BaseException,
    tb: TracebackType | None,
    exception_converter: ExceptionConverter,
) -> tuple[LoggedException, bool]:
    """例外1件の変換結果と発生位置をまとめ、内部原因を集約済みかと合わせて返す。"""
    converted = exception_converter(exc)
    logged: LoggedException = {
        "error_class": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "error_message": converted.message,
        "frames": extract_exception_frames(tb),
    }
    if converted.error_details is not None:
        logged["error_details"] = converted.error_details
    return logged, converted.cause_is_aggregated
