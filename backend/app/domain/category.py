"""Category エンティティの値オブジェクト。

CategorySlug: カテゴリの URL セーフな識別子 (例: "ai_ml", "semiconductor")。
CategoryName: カテゴリの日本語表示名 (例: "AI・ML", "半導体")。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ConfigDict, RootModel, field_validator

_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,49}$")
_NAME_PATTERN = re.compile(r"^[\w・ \-]+$", re.UNICODE)
_NAME_MAX_LENGTH = 50


class CategorySlug(RootModel[str]):
    """URL セーフなカテゴリ識別子。

    Invariants:
    - 先頭は小文字または数字
    - 使用可能な文字は小文字・数字・アンダースコアのみ
    - 1-50 文字
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
        if not _SLUG_PATTERN.fullmatch(v):
            msg = (
                "Category slug must start with a lowercase letter or digit, "
                f"contain only [a-z0-9_], and be 1-50 chars. Got: {v!r}"
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
        return f"CategorySlug({self.root!r})"


class CategoryName(RootModel[str]):
    """カテゴリの日本語表示名。

    Invariants:
    - ワード文字・中黒 (・)・空白・ハイフンで構成
    - 空文字列や空白のみは不可
    - 1-50 文字
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
            msg = "CategoryName must not be empty"
            raise ValueError(msg)
        if len(v) > _NAME_MAX_LENGTH:
            msg = f"CategoryName must be at most {_NAME_MAX_LENGTH} chars, got {len(v)}"
            raise ValueError(msg)
        if not _NAME_PATTERN.fullmatch(v):
            msg = (
                "CategoryName can only contain word characters, ・, spaces, "
                f"or hyphens. Got: {v!r}"
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
        return f"CategoryName({self.root!r})"
