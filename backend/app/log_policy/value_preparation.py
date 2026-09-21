from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.log_policy.base import BASE_MASK, normalize_key
from app.log_policy.budget import TEXT_LIMIT, LogEventBudget
from app.log_policy.diagnostics import LogProcessingDiagnostics
from app.log_policy.mask import mask_assignments
from app.log_policy.sanitize import sanitize_text

DEPTH_LIMIT = 10
EXCEPTION_DEPTH_LIMIT = 19


class _ValueMarker(Enum):
    """置換後の固定文字列は入力の文字列ではないため、サニタイズ・マスクの対象にしない。"""

    LIMIT = "[limit]"
    UNSUPPORTED = "[unsupported]"
    CYCLE = "[cycle]"
    NON_FINITE = "[non-finite]"
    NON_STRING_KEY = "[non-string-key]"


def is_supported_value(value: Any) -> bool:
    """独自の走査・文字列化を避けるため、出力対象の組み込み型だけを認める。"""
    return type(value) in {type(None), bool, int, float, str, dict, list, tuple}


@dataclass
class LogValuePreparer:
    """トップレベルの項目名は扱わず、項目の値を構造検査・サニタイズ・マスクして出力用に整える。"""

    deny: frozenset[str]
    mask: frozenset[str] = BASE_MASK
    budget: LogEventBudget = field(default_factory=LogEventBudget, kw_only=True)
    diagnostics: LogProcessingDiagnostics = field(
        default_factory=LogProcessingDiagnostics, kw_only=True
    )
    active_container_ids: set[int] = field(default_factory=set)

    def prepare_field_value(
        self,
        field_value: Any,
        *,
        depth_limit: int = DEPTH_LIMIT,
    ) -> Any:
        """指定された深さ上限で値を構造検査し、サニタイズ・マスクして出力用に整える。"""
        inspected_value = self.inspect_value(field_value, depth_limit=depth_limit)
        return self.prepare_text_values(inspected_value)

    def inspect_value(
        self,
        value: Any,
        depth: int = 0,
        *,
        depth_limit: int = DEPTH_LIMIT,
    ) -> Any:
        """深さ・型・循環を確認し、値の種類に応じた検査へ振り分ける。"""
        if depth > depth_limit:
            return _ValueMarker.LIMIT

        if not is_supported_value(value):
            return _ValueMarker.UNSUPPORTED

        value_type = type(value)

        if value_type in {type(None), bool, int, float, str}:
            return self.inspect_scalar(value)

        # 辞書・配列では、現在の経路に戻る参照だけを循環として扱う。
        if id(value) in self.active_container_ids:
            return _ValueMarker.CYCLE

        self.active_container_ids.add(id(value))
        try:
            if value_type is dict:
                return self.inspect_dictionary(value, depth, depth_limit=depth_limit)

            return self.inspect_sequence(value, depth, depth_limit=depth_limit)
        finally:
            self.active_container_ids.remove(id(value))

    def inspect_scalar(
        self, value: None | bool | int | float | str
    ) -> None | bool | int | float | str | _ValueMarker:
        """子を持たない値を検査し、長すぎる文字列・大きすぎる整数・非有限の数は、ログのその部分だけをマーカーに置き換える。"""
        value_type = type(value)

        if value is None or value_type is bool:
            return value

        if value_type is int:
            if value.bit_length() > 4096:
                return _ValueMarker.LIMIT
            return value

        if value_type is float:
            return value if math.isfinite(value) else _ValueMarker.NON_FINITE

        text_length = len(value)
        if text_length > TEXT_LIMIT:
            return _ValueMarker.LIMIT
        self.budget.check_and_count_text_chars(text_length)
        return value

    def inspect_dictionary(
        self,
        dictionary: dict[Any, Any],
        depth: int,
        *,
        depth_limit: int = DEPTH_LIMIT,
    ) -> dict[str, Any] | _ValueMarker:
        """辞書を検査し、denyのキーと長すぎるキーは値ごと落として診断へ記録し、文字列でないキーがあれば辞書ごとマーカーに置き換える。"""
        if any(type(key) is not str for key in dictionary):
            return _ValueMarker.NON_STRING_KEY

        inspected_dictionary: dict[str, Any] = {}

        for key, child_value in dictionary.items():
            self.budget.check_and_count_log_items(1)

            if len(key) > TEXT_LIMIT:
                self.diagnostics.record_nested_key_limit_reached()
                continue

            if normalize_key(key) in self.deny:
                self.diagnostics.record_nested_denied()
                continue

            self.budget.check_and_count_text_chars(len(key))

            inspected_value = self.inspect_value(
                child_value, depth + 1, depth_limit=depth_limit
            )
            inspected_dictionary[key] = inspected_value

        return inspected_dictionary

    def inspect_sequence(
        self,
        sequence: list[Any] | tuple[Any, ...],
        depth: int,
        *,
        depth_limit: int = DEPTH_LIMIT,
    ) -> list[Any]:
        """配列を検査し、要素は落とさず順序と件数を保ったまま、問題のある要素だけをマーカーに置き換える。"""
        inspected_items: list[Any] = []

        for child_value in sequence:
            self.budget.check_and_count_log_items(1)

            inspected_value = self.inspect_value(
                child_value, depth + 1, depth_limit=depth_limit
            )
            inspected_items.append(inspected_value)

        return inspected_items

    def prepare_text_values(self, value: Any) -> Any:
        """検査済みの構造だけを辿り、文字列と辞書キーを一度ずつ出力用に整える。"""
        if type(value) is _ValueMarker:
            return value.value
        if type(value) is str:
            sanitized_text = sanitize_text(value)
            return mask_assignments(sanitized_text, mask=self.mask)
        if type(value) is dict:
            prepared_dictionary: dict[str, Any] = {}
            for key, child_value in value.items():
                sanitized_key = sanitize_text(key)
                masked_key = mask_assignments(sanitized_key, mask=self.mask)
                prepared_dictionary[masked_key] = self.prepare_text_values(child_value)
            return prepared_dictionary
        if type(value) is list:
            return [self.prepare_text_values(item) for item in value]
        return value
