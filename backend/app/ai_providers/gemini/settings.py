"""Gemini通信の待機時間を宣言する。"""

from dataclasses import dataclass, fields
from math import isfinite


@dataclass(frozen=True, slots=True)
class GeminiConnectionSettings:
    """秘密情報やモデル仕様を含まない不変の通信設定。"""

    connect_timeout: float = 3.0
    read_timeout: float = 10.0
    write_timeout: float = 10.0
    pool_timeout: float = 3.0

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("timeouts must be numbers")
            if not isfinite(value) or value <= 0:
                raise ValueError("timeouts must be positive and finite")
