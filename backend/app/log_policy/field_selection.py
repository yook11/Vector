"""トップレベルの項目名を検査し、ログに残す項目を選ぶ。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.log_policy.base import normalize_key
from app.log_policy.budget import TEXT_LIMIT, LogEventBudget
from app.log_policy.diagnostics import LogProcessingDiagnostics

# 例外からの生成は共通ロガーが担うため、呼び出し側で定義した同名フィールドは除外する。
_EXCEPTION_OUTPUT_ONLY_FIELDS = frozenset({"error_details", "causes", "exceptions"})

_EXCLUDED_FIELDS = frozenset(
    {
        "stack",
        "stack_info",
        "exception",
        "_record",
        "_from_structlog",
        "_log_policy_rules",  # 旧形式の内部キーも出力しない。
        "exc_info",
    }
)


@dataclass
class LogFieldSelector:
    """値には触れず名前だけで判定し、このログが受け付けるトップレベルの項目を取り出す。"""

    allow: frozenset[str]
    deny: frozenset[str]
    diagnostics: LogProcessingDiagnostics
    budget: LogEventBudget = field(kw_only=True)

    def select_fields(self, event_dict: Mapping[Any, Any]) -> dict[str, Any]:
        """入力ログの項目を一つずつ数えて判定し、受け付けた項目だけを入力順に返す。"""
        selected_fields: dict[str, Any] = {}

        for field_name, field_value in event_dict.items():
            self.budget.check_and_count_log_items(1)

            if type(field_name) is not str:
                self.diagnostics.record_unregistered()
                continue

            if field_name in _EXCLUDED_FIELDS:
                continue

            if len(field_name) > TEXT_LIMIT:
                self.diagnostics.record_top_level_limit_reached()
                continue

            normalized_name = normalize_key(field_name)
            if normalized_name in _EXCEPTION_OUTPUT_ONLY_FIELDS:
                continue

            # allowと重なってもdenyを優先し、除外の理由を名前付きで診断に残す。
            if normalized_name in self.deny:
                self.diagnostics.record_top_level_denied(field_name)
                continue

            if normalized_name not in self.allow:
                self.diagnostics.record_unregistered()
                continue

            self.budget.check_and_count_text_chars(len(field_name))
            selected_fields[field_name] = field_value

        return selected_fields
