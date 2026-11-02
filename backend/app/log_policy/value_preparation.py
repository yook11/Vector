from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.log_policy.base import BASE_MASK, normalize_key
from app.log_policy.budget import TEXT_LIMIT, LogEventBudget
from app.log_policy.diagnostics import LogProcessingDiagnostics
from app.log_policy.leak_prevention import prevent_leaks
from app.log_policy.sanitize import sanitize_field_value

type ValueRejectionReason = Literal["text_length_exceeded", "value_shape_invalid"]

_MASKED = "***"


class LogValueRejected(Exception):
    """値の約束違反を通知し、ログ一件の処理を中止する。"""

    def __init__(self, reason: ValueRejectionReason) -> None:
        super().__init__(reason)
        self.reason: ValueRejectionReason = reason


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

    def count_field_value(
        self, field_value: Any, *, field_name: str | None = None
    ) -> Any:
        """通常の項目の値をスカラーかスカラーのlistに限って予算に数えて加工に渡す値を返し、約束違反と共有予算の超過は例外のまま呼び出し側へ伝える。"""
        # マスクする値は中身を出さないため、形を検査せずに数えない。
        if field_name is not None and normalize_key(field_name) in self.mask:
            return field_value
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

    def apply_policy(self, value: Any, *, field_name: str | None = None) -> Any:
        """ログに出す形の木をたどり、deny・mask・項目別サニタイズ・情報漏洩防止をかける。"""
        # 数える段はmaskの項目を飛ばすだけなので、伏せるのはここで行う。
        if field_name is not None and normalize_key(field_name) in self.mask:
            return _MASKED
        # 配列の要素は親の項目名を引き継ぎ、辞書に入ったら子のキーで選び直す。
        if isinstance(value, dict):
            prepared_dictionary: dict[str, Any] = {}
            for key, child_value in value.items():
                if normalize_key(key) in self.deny:
                    self.diagnostics.record_nested_denied()
                    continue
                prepared_key = prevent_leaks(key)
                prepared_dictionary[prepared_key] = self.apply_policy(
                    child_value, field_name=key
                )
            return prepared_dictionary
        if isinstance(value, list):
            return [self.apply_policy(item, field_name=field_name) for item in value]
        return self._prepare_text(value, field_name=field_name)

    def _prepare_text(self, value: Any, *, field_name: str | None) -> Any:
        """子を持たない値に項目別サニタイズをかけ、文字列には情報漏洩防止もかける。"""
        if field_name is not None:
            normalized_field_name = normalize_key(field_name)
            if normalized_field_name in self.sanitize:
                sanitized_value = sanitize_field_value(normalized_field_name, value)
                # 入力型が合わない値は固定マーカーになるため、情報漏洩防止へ渡さない。
                if type(value) is not str:
                    return sanitized_value
                value = sanitized_value
        if isinstance(value, str):
            return prevent_leaks(value)
        return value
