"""トップレベルの項目名を検査し、ログに残す項目を選ぶ。"""

from dataclasses import dataclass
from typing import Any

from app.log_policy.base import normalize_key
from app.log_policy.budget import TEXT_LIMIT
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
    """値には触れず、項目名の制約とdeny・allowを判定する。"""

    allow: frozenset[str]
    deny: frozenset[str]
    diagnostics: LogProcessingDiagnostics

    def select(self, field_name: Any) -> bool:
        """項目名を検査してdenyをallowより優先して選別し、除外の理由を診断へ記録する。"""
        if type(field_name) is not str:
            self.diagnostics.record_unregistered()
            return False

        if field_name in _EXCLUDED_FIELDS:
            return False

        if len(field_name) > TEXT_LIMIT:
            self.diagnostics.record_top_level_limit_reached()
            return False

        normalized_name = normalize_key(field_name)
        if normalized_name in _EXCEPTION_OUTPUT_ONLY_FIELDS:
            return False
        if normalized_name in self.deny:
            self.diagnostics.record_top_level_denied(field_name)
            return False

        if normalized_name not in self.allow:
            self.diagnostics.record_unregistered()
            return False

        return True
