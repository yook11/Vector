"""Keyword エンティティの値オブジェクト。

KeywordName: セクター内の特定技術やテーマを表すタグ。
例: "large language model", "AI/ML", "C++", "Node.js", "量子エラー訂正"
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ConfigDict, RootModel, field_validator

_KEYWORD_PATTERN = re.compile(r"^(?=.*\w)[\w \-\.&/+#]+$", re.UNICODE)
_KEYWORD_MAX_LENGTH = 100


class KeywordName(RootModel[str]):
    """セクター内の技術やテーマを表すタグ名。

    Invariants:
    - 少なくとも 1 つのワード文字 (\\w) を含む
    - 使用可能文字は Unicode ワード文字・空白・ハイフン・ドット・
      &・/・+・#
    - トリム後 1-100 文字
    - 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    @field_validator("root", mode="before")
    @classmethod
    def _validate(cls, v: Any) -> str:
        if not isinstance(v, str):
            msg = f"Expected str, got {type(v).__name__}"
            raise ValueError(msg)
        v = v.strip()
        if not v:
            msg = "KeywordName must not be empty"
            raise ValueError(msg)
        if len(v) > _KEYWORD_MAX_LENGTH:
            msg = (
                f"KeywordName must be at most {_KEYWORD_MAX_LENGTH} chars, got {len(v)}"
            )
            raise ValueError(msg)
        if not _KEYWORD_PATTERN.fullmatch(v):
            msg = (
                "KeywordName can only contain letters, numbers, spaces, "
                "hyphens, dots, &, /, +, #, and underscores. "
                f"Got: {v!r}"
            )
            raise ValueError(msg)
        return v

    @property
    def value(self) -> str:
        """後方互換用。新規コードでは .root を使用すること。"""
        return self.root

    def __str__(self) -> str:
        return self.root

    def __repr__(self) -> str:
        return f"KeywordName({self.root!r})"
