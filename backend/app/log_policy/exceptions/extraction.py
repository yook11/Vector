"""例外の原因とグループを上限内で探索し、変換結果をログフィールドへまとめる。

秘密情報のサニタイズとマスクは値準備が担当する。
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, NotRequired, TypedDict

from app.log_policy.exceptions.conversion import convert_exception
from app.log_policy.exceptions.types import ErrorDetails, ExceptionConverter

# 外側を含めた例外の件数。frame・文字数・診断情報の項目数は例外全体の合計で数える。
EXCEPTION_LIMIT = 8
FRAME_TOTAL_LIMIT = 64
TEXT_LENGTH_LIMIT = 4000
TEXT_TOTAL_LIMIT = 16000
DETAILS_ITEM_LIMIT = 32

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
class _ExceptionConversion:
    """例外全体を共通の形式へ変換し、件数・frame・文字数・診断情報の残りを共有する。"""

    exception_converter: ExceptionConverter
    remaining_exceptions: int
    remaining_frames: int
    remaining_text: int
    remaining_details_items: int
    related: list[RelatedException] = field(default_factory=list)

    def describe(
        self, exc: BaseException, tb: TracebackType | None
    ) -> tuple[LoggedException, bool]:
        """例外1件を残りの量に収めて変換し、内部原因を集約済みかと合わせて返す。"""
        converted = self.exception_converter(exc)
        logged: LoggedException = {
            "error_class": self.take_text(
                f"{type(exc).__module__}.{type(exc).__qualname__}"
            ),
            "error_message": self.take_text(converted.message),
            "frames": self.take_frames(tb),
        }
        if converted.error_details is not None:
            logged["error_details"] = self.take_details(converted.error_details)
        return logged, converted.cause_is_aggregated

    def follow(
        self, exc: BaseException, *, position: int | None, ancestors: set[int]
    ) -> None:
        """前の例外を先に、グループのメンバーを元の順序で後に並べる。"""
        if exc.__cause__ is not None:
            self.append(exc.__cause__, "cause", parent=position, ancestors=ancestors)
        elif exc.__context__ is not None and not exc.__suppress_context__:
            self.append(
                exc.__context__, "context", parent=position, ancestors=ancestors
            )

        if isinstance(exc, BaseExceptionGroup):
            for member in exc.exceptions:
                omitted = self.append(
                    member, "member", parent=position, ancestors=ancestors
                )
                if omitted:
                    break

    def append(
        self,
        exc: BaseException,
        relation: ExceptionRelation,
        *,
        parent: int | None,
        ancestors: set[int],
    ) -> bool:
        """子の属性や型を調べる前に件数を確認して1件並べ、上限で省略したかを返す。"""
        if self.remaining_exceptions <= 0:
            self.related.append(
                {"parent": parent, "relation": relation, "exception": "[limit]"}
            )
            return True

        # 循環参照の確認も数え、同じ参照が並ぶグループの走査を制限する。
        self.remaining_exceptions -= 1
        if id(exc) in ancestors:
            self.related.append(
                {"parent": parent, "relation": relation, "exception": "[cycle]"}
            )
            return False

        logged, cause_is_aggregated = self.describe(exc, exc.__traceback__)
        position = len(self.related)
        self.related.append(
            {"parent": parent, "relation": relation, "exception": logged}
        )
        if not cause_is_aggregated:
            self.follow(exc, position=position, ancestors=ancestors | {id(exc)})
        return False

    def take_text(self, text: str) -> str:
        """単一の上限と合計の残りに収まる文字列だけを残し、収まらなければ[limit]にする。"""
        if len(text) > TEXT_LENGTH_LIMIT or len(text) > self.remaining_text:
            return "[limit]"
        self.remaining_text -= len(text)
        return text

    def take_frames(
        self, tb: TracebackType | None
    ) -> list[ExceptionLogFrame] | Literal["[limit]"]:
        """合計の残りに収まるframeだけを抽出し、収まらなければ部分リストを残さない。"""
        frames: list[ExceptionLogFrame] = []
        while tb is not None:
            if len(frames) >= self.remaining_frames:
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
        self.remaining_frames -= len(frames)
        for frame in frames:
            frame["file"] = self.take_text(frame["file"])
            frame["function"] = self.take_text(frame["function"])
        return frames

    def take_details(self, details: ErrorDetails) -> ErrorDetails | Literal["[limit]"]:
        """項目数と文字数の残りに収まる診断情報だけを残し、収まらなければ全体を[limit]にする。"""
        inventory = _inventory_details(details, item_limit=self.remaining_details_items)
        if inventory is None:
            return "[limit]"
        keys, item_count = inventory
        # キーは置き換えられないため、値より先に収まるかを確かめて数える。
        key_length = sum(len(key) for key in keys)
        if (
            any(len(key) > TEXT_LENGTH_LIMIT for key in keys)
            or key_length > self.remaining_text
        ):
            return "[limit]"
        self.remaining_details_items -= item_count
        self.remaining_text -= key_length
        return {key: self.take_detail_texts(value) for key, value in details.items()}

    def take_detail_texts(self, value: object) -> object:
        """診断情報の構造を保ったまま、文字列の値だけを残りの文字数に収める。"""
        if isinstance(value, str):
            return self.take_text(value)
        if isinstance(value, Mapping):
            return {key: self.take_detail_texts(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.take_detail_texts(item) for item in value]
        return value


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

    conversion = _ExceptionConversion(
        exception_converter=exception_converter,
        remaining_exceptions=EXCEPTION_LIMIT - 1,
        remaining_frames=FRAME_TOTAL_LIMIT,
        remaining_text=TEXT_TOTAL_LIMIT,
        remaining_details_items=DETAILS_ITEM_LIMIT,
    )
    logged, cause_is_aggregated = conversion.describe(exc, tb)
    fields: ExceptionLogFields = {**logged}
    if cause_is_aggregated:
        return fields

    conversion.follow(exc, position=None, ancestors={id(exc)})
    if conversion.related:
        fields["related_exceptions"] = conversion.related
    return fields


def _inventory_details(
    details: object, *, item_limit: int
) -> tuple[list[str], int] | None:
    """診断情報のキーと項目数を集め、項目数が上限を超えた時点でNoneを返す。"""
    keys: list[str] = []
    item_count = 0
    pending: list[object] = [details]
    while pending:
        value = pending.pop()
        if isinstance(value, Mapping):
            children = list(value.values())
            keys.extend(str(key) for key in value)
        elif isinstance(value, list | tuple):
            children = list(value)
        else:
            continue
        item_count += len(children)
        # 項目数の上限で打ち切り、循環した診断情報でも走査を終わらせる。
        if item_count > item_limit:
            return None
        pending.extend(children)
    return keys, item_count
