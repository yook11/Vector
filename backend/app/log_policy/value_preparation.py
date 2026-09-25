from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.log_policy.base import BASE_MASK, normalize_key
from app.log_policy.budget import TEXT_LIMIT, LogEventBudget
from app.log_policy.diagnostics import LogProcessingDiagnostics
from app.log_policy.leak_prevention import prevent_credential_leaks
from app.log_policy.sanitize import sanitize_field_value

DEPTH_LIMIT = 10
EXCEPTION_DEPTH_LIMIT = 19


class _ValueMarker(Enum):
    """置換後の固定文字列は入力の文字列ではないため、サニタイズ・情報漏洩防止の対象にしない。"""

    MASKED = "***"
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
    """ログの構造を捜査して、出力用に整える"""

    deny: frozenset[str]
    mask: frozenset[str] = BASE_MASK
    sanitize: frozenset[str] = field(default_factory=frozenset, kw_only=True)
    budget: LogEventBudget = field(default_factory=LogEventBudget, kw_only=True)
    diagnostics: LogProcessingDiagnostics = field(
        default_factory=LogProcessingDiagnostics, kw_only=True
    )
    active_container_ids: set[int] = field(default_factory=set)

    def prepare_field_value(
        self,
        field_value: Any,
        *,
        field_name: str | None = None,
        depth_limit: int = DEPTH_LIMIT,
    ) -> Any:
        """値の準備の入口で、検査で形を確定させてから文字列を加工し、共有予算の超過は例外のまま呼び出し側へ伝える。"""
        inspected_value = self.inspect_value(
            field_value, field_name=field_name, depth_limit=depth_limit
        )
        return self.prepare_text_values(inspected_value, field_name=field_name)

    def inspect_value(
        self,
        value: Any,
        depth: int = 0,
        *,
        field_name: str | None = None,
        depth_limit: int = DEPTH_LIMIT,
    ) -> Any:
        """値の木を再帰的にたどる中心で、位置ごとに打ち切るか子へ進むかを決める。"""
        # マスク対象の項目を先に確認して、中身を見ずに丸ごと伏せる。
        if field_name is not None:
            normalized_field_name = normalize_key(field_name)
            if normalized_field_name in self.mask:
                return _ValueMarker.MASKED

        # 深すぎる位置と扱えない型は、その位置だけ打ち切る。
        if depth > depth_limit:
            return _ValueMarker.LIMIT

        if not is_supported_value(value):
            return _ValueMarker.UNSUPPORTED

        value_type = type(value)

        # 子を持たない値は、木の末端としてここで検査を終える。
        if value_type in {type(None), bool, int, float, str}:
            return self.inspect_scalar(value)

        # 辞書・配列は経路に載せて子へ進み、経路上に戻る参照だけを循環とする。
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
        """木の末端の値が、ログに出せる大きさと種類かを確かめる。"""
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
        """辞書のキーごとに残すか落とすかを決め、残したキーの値だけを子として検査を続ける。"""
        if any(type(key) is not str for key in dictionary):
            return _ValueMarker.NON_STRING_KEY

        inspected_dictionary: dict[str, Any] = {}

        for key, child_value in dictionary.items():
            self.budget.check_and_count_log_items(1)

            # 長すぎるキーとdenyのキーは、値を見ずに落として診断に残す。
            if len(key) > TEXT_LIMIT:
                self.diagnostics.record_nested_key_limit_reached()
                continue

            if normalize_key(key) in self.deny:
                self.diagnostics.record_nested_denied()
                continue

            self.budget.check_and_count_text_chars(len(key))

            inspected_value = self.inspect_value(
                child_value, depth + 1, field_name=key, depth_limit=depth_limit
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
        """配列は要素を落とさず、各要素を子として同じ検査にかける。"""
        inspected_items: list[Any] = []

        for child_value in sequence:
            self.budget.check_and_count_log_items(1)

            inspected_value = self.inspect_value(
                child_value, depth + 1, depth_limit=depth_limit
            )
            inspected_items.append(inspected_value)

        return inspected_items

    def prepare_text_values(self, value: Any, *, field_name: str | None = None) -> Any:
        """形が確定した木をもう一度たどり、文字列だけを項目別サニタイズと情報漏洩防止で加工する。"""
        if type(value) is _ValueMarker:
            return value.value
        # 配列の要素は親の項目名を引き継ぎ、辞書に入ったら子のキーで選び直す。
        if type(value) is dict:
            prepared_dictionary: dict[str, Any] = {}
            for key, child_value in value.items():
                prepared_key = prevent_credential_leaks(key)
                prepared_dictionary[prepared_key] = self.prepare_text_values(
                    child_value, field_name=key
                )
            return prepared_dictionary
        if type(value) is list:
            return [
                self.prepare_text_values(item, field_name=field_name) for item in value
            ]
        if field_name is not None:
            normalized_field_name = normalize_key(field_name)
            if normalized_field_name in self.sanitize:
                sanitized_value = sanitize_field_value(normalized_field_name, value)
                # 入力型が合わない値は固定マーカーになるため、情報漏洩防止へ渡さない。
                if type(value) is not str:
                    return sanitized_value
                value = sanitized_value
        if type(value) is str:
            return prevent_credential_leaks(value)
        return value
