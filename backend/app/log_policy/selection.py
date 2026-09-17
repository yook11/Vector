"""ログ入力から出力するフィールドを選び、値の変換は行わない。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.log_policy.rules import BASE_DENY, LogPolicyRules, normalize_key

POLICY_BIND_KEY = "_log_policy"
METADATA_FIELDS = frozenset({"event", "level", "timestamp", "logger", "logger_name"})
# 生スタックや内部制御値を renderer へ流さない。
_EXCLUDED_FIELDS = frozenset(
    {
        "stack",
        "stack_info",
        "exception",
        "_record",
        "_from_structlog",
        POLICY_BIND_KEY,
        "exc_info",
    }
)
FIELD_LIMIT = 1000


@dataclass
class FieldSelection:
    fields: dict[str, Any] = field(default_factory=dict)
    denied_keys: list[str] = field(default_factory=list)
    unregistered_count: int = 0
    truncated: bool = False

    def diagnostics(self) -> dict[str, Any]:
        """入力由来の未登録名を出さず、採否の診断だけを返す。"""
        result: dict[str, Any] = {}
        if self.denied_keys:
            result["_denied_keys"] = self.denied_keys
        if self.unregistered_count:
            result["_unregistered_count"] = self.unregistered_count
        if self.truncated:
            result["_policy_truncated"] = True
        return result


def select_fields(
    fields: Mapping[str, Any], rules: LogPolicyRules | None
) -> FieldSelection:
    """deny 優先で選別し、除外する値には触れない。"""
    deny = rules.effective_deny if rules is not None else BASE_DENY
    allow = rules.normalized_allow if rules is not None else frozenset()
    selection = FieldSelection()
    for index, (key, value) in enumerate(fields.items()):
        if index >= FIELD_LIMIT:
            selection.truncated = True
            break
        if type(key) is not str:
            selection.unregistered_count += 1
            continue
        if key in _EXCLUDED_FIELDS:
            continue
        normalized = normalize_key(key)
        if normalized in deny:
            selection.denied_keys.append(key)
        elif normalized in allow or key in METADATA_FIELDS:
            selection.fields[key] = value
        else:
            selection.unregistered_count += 1
    return selection
