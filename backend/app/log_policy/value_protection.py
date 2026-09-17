"""選別済みの値を安全な JSON 相当型へ変換する。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.log_policy.rules import normalize_key
from app.log_policy.sanitize import TEXT_LIMIT, sanitize_text
from app.log_policy.selection import METADATA_FIELDS

VALUE_LIMIT = 1000
DEPTH_LIMIT = 10


@dataclass
class ValueProtector:
    deny: frozenset[str]
    remaining: int = VALUE_LIMIT
    denied: int = 0
    ancestors: set[int] = field(default_factory=set)

    def scrub(self, value: Any, depth: int = 0) -> Any:
        if self.remaining <= 0 or depth > DEPTH_LIMIT:
            return "[limit]"
        self.remaining -= 1
        kind = type(value)
        if value is None or kind is bool:
            return value
        if kind is int:
            return value if value.bit_length() <= 4096 else "[limit]"
        if kind is float:
            return value if math.isfinite(value) else "[non-finite]"
        if kind is str:
            return sanitize_text(value, self.deny)[:TEXT_LIMIT]
        # subclass の独自 iterator / repr / __structlog__ も実行しない。
        if kind not in {dict, list, tuple}:
            return "[unsupported]"
        if id(value) in self.ancestors:
            return "[cycle]"
        self.ancestors.add(id(value))
        try:
            if kind is dict:
                result: dict[str, Any] = {}
                for index, (key, item) in enumerate(value.items()):
                    if self.remaining <= 0 or index >= VALUE_LIMIT:
                        result["_policy_truncated"] = True
                        break
                    if type(key) is not str or normalize_key(key) in self.deny:
                        self.remaining -= 1
                        self.denied += 1
                        continue
                    result[sanitize_text(key, self.deny)[:TEXT_LIMIT]] = self.scrub(
                        item, depth + 1
                    )
                return result
            items: list[Any] = []
            for item in value:
                if self.remaining <= 0:
                    items.append("[limit]")
                    break
                items.append(self.scrub(item, depth + 1))
            return items
        finally:
            self.ancestors.remove(id(value))

    def protect_fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        """予約情報の型制約とイベント全体の値予算を維持する。"""
        return {
            key: "[unsupported]"
            if key in METADATA_FIELDS and type(value) is not str
            else self.scrub(value)
            for key, value in fields.items()
        }
