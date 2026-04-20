"""エンティティ（AI 抽出対象）の値オブジェクト。

EntityName: 記事中に登場する固有名。表示用に元の大文字小文字を保つ。
EntityType: エンティティ種別ラベル。正規化のため小文字化する。
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, RootModel, field_validator

_NAME_MAX_LENGTH = 200
_TYPE_MAX_LENGTH = 50


class EntityName(RootModel[str]):
    """エンティティの固有名。

    Invariants:
    - トリム後 1-200 文字
    - 大文字小文字は保持（"NVIDIA" を表示用にそのまま維持）
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
            msg = "EntityName must not be empty"
            raise ValueError(msg)
        if len(v) > _NAME_MAX_LENGTH:
            msg = f"EntityName must be at most {_NAME_MAX_LENGTH} chars, got {len(v)}"
            raise ValueError(msg)
        return v

    def __str__(self) -> str:
        return self.root

    def __repr__(self) -> str:
        return f"EntityName({self.root!r})"


class EntityType(RootModel[str]):
    """エンティティ種別ラベル。

    Invariants:
    - トリム後 1-50 文字
    - 小文字に正規化
    - 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    @field_validator("root", mode="before")
    @classmethod
    def _validate(cls, v: Any) -> str:
        if not isinstance(v, str):
            msg = f"Expected str, got {type(v).__name__}"
            raise ValueError(msg)
        v = v.strip().lower()
        if not v:
            msg = "EntityType must not be empty"
            raise ValueError(msg)
        if len(v) > _TYPE_MAX_LENGTH:
            msg = f"EntityType must be at most {_TYPE_MAX_LENGTH} chars, got {len(v)}"
            raise ValueError(msg)
        return v

    def __str__(self) -> str:
        return self.root

    def __repr__(self) -> str:
        return f"EntityType({self.root!r})"
