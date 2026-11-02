from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.log_policy.base import normalize_key
from app.log_policy.budget import TEXT_LIMIT, LogEventBudget
from app.log_policy.diagnostics import LogProcessingDiagnostics
from app.log_policy.leak_prevention import prevent_leaks

type ValueRejectionReason = Literal["text_length_exceeded", "value_shape_invalid"]


class LogValueRejected(Exception):
    """値の約束違反を通知し、ログ一件の処理を中止する。"""

    def __init__(self, reason: ValueRejectionReason) -> None:
        super().__init__(reason)
        self.reason: ValueRejectionReason = reason


@dataclass
class LogValuePreparer:
    """ログの構造を捜査して、出力用に整える"""

    deny: frozenset[str]
    budget: LogEventBudget = field(default_factory=LogEventBudget, kw_only=True)
    diagnostics: LogProcessingDiagnostics = field(
        default_factory=LogProcessingDiagnostics, kw_only=True
    )

    def count_field_value(self, field_value: Any) -> Any:
        """通常の項目の値をスカラーかスカラーのlistに限って予算に数えて加工に渡す値を返し、約束違反と共有予算の超過は例外のまま呼び出し側へ伝える。"""
        # 入れ子を数えるには深さと循環の扱いが要るため、辞書と入れ子は受けない。
        if isinstance(field_value, dict):
            raise LogValueRejected("value_shape_invalid")
        if not isinstance(field_value, list | tuple):
            self._count_scalar(field_value)
            return field_value
        # ポリシーによる加工は数えた要素だけにかけるため、
        # 数えた要素を新しいlistに入れて渡す。
        counted_items: list[Any] = []
        for item in field_value:
            self.budget.check_and_count_log_items(1)
            if isinstance(item, dict | list | tuple):
                raise LogValueRejected("value_shape_invalid")
            self._count_scalar(item)
            counted_items.append(item)
        return counted_items

    def _count_scalar(self, value: Any) -> None:
        """上限を超える文字列は数える前に止め、収まる文字列の文字数を数える。"""
        if isinstance(value, str):
            if len(value) > TEXT_LIMIT:
                raise LogValueRejected("text_length_exceeded")
            self.budget.check_and_count_text_chars(len(value))

    def apply_policy(self, value: Any) -> Any:
        """ログに出す形の木をたどり、入れ子のキーにdenyを、文字列に情報漏洩防止をかける。"""
        if isinstance(value, dict):
            prepared_dictionary: dict[str, Any] = {}
            for key, child_value in value.items():
                if normalize_key(key) in self.deny:
                    self.diagnostics.record_nested_denied()
                    continue
                prepared_key = prevent_leaks(key)
                prepared_dictionary[prepared_key] = self.apply_policy(child_value)
            return prepared_dictionary
        if isinstance(value, list):
            return [self.apply_policy(item) for item in value]
        if isinstance(value, str):
            return prevent_leaks(value)
        return value
