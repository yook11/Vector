"""エンティティ（AI 抽出対象）の値オブジェクト。

EntityName: 記事中に登場する固有名。表示用に NFKC + 空白整形した文字列を保持し、
            重複検出・JOIN 用に match_key (lower 化) を併せて提供する。
            casing は保持する（"NVIDIA" を表示用にそのまま維持）。
EntityType: エンティティ種別ラベル。正規化のため小文字化する。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from pydantic import ConfigDict, RootModel, computed_field, field_validator

_NAME_MAX_LENGTH = 200
_TYPE_MAX_LENGTH = 50
_RAW_TYPE_MAX_LENGTH = 30

# 連続する任意の空白文字 (タブ/改行/NBSP/全角空白を含む) を単一半角空白に統合する。
_WHITESPACE_RUN = re.compile(r"\s+")


class EntityName(RootModel[str]):
    """エンティティの固有名。

    Invariants:
    - NFKC 正規化 + 前後空白除去 + 連続空白を単一半角空白に統合
    - 正規化後 1-200 文字
    - 大文字小文字は保持（"NVIDIA" を表示用にそのまま維持）
    - 生成後は不変

    `match_key` プロパティは正規化済み文字列を str.lower() した値を返す。
    重複検出・JOIN キーに使う。casefold は使わない (AI 抽出 casing は文脈情報)。
    """

    model_config = ConfigDict(frozen=True)

    @field_validator("root", mode="before")
    @classmethod
    def _validate(cls, v: Any) -> str:
        if not isinstance(v, str):
            msg = f"Expected str, got {type(v).__name__}"
            raise ValueError(msg)
        normalized = unicodedata.normalize("NFKC", v)
        normalized = _WHITESPACE_RUN.sub(" ", normalized).strip()
        if not normalized:
            msg = "EntityName must not be empty"
            raise ValueError(msg)
        if len(normalized) > _NAME_MAX_LENGTH:
            msg = (
                f"EntityName must be at most {_NAME_MAX_LENGTH} chars, "
                f"got {len(normalized)}"
            )
            raise ValueError(msg)
        return normalized

    @computed_field  # type: ignore[prop-decorator]
    @property
    def match_key(self) -> str:
        """重複検出・JOIN 用の小文字化キー (str.lower())。"""
        return self.root.lower()

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


# ``EntitySurface`` は表示用の固有名表面形 (Stage 1 観察台帳の surface)。
# ``EntityName`` を再利用して不変条件を二重定義しない (NFKC + 空白整形 + 200 字 +
# casing 保持 + match_key)。``type`` 文ではなく単純な束縛にすることで
# ``EntitySurface is EntityName`` を保証する (Pydantic field annotation でも
# alias として透過に動作)。
EntitySurface = EntityName


class EntityRawType(RootModel[str]):
    """Stage 1 観察用の type ラベル (Phase 1B α-1)。

    Invariants:
    - NFKC 正規化 + 前後空白除去 + 連続空白を単一半角空白に統合
    - 正規化後 1-30 文字
    - 大文字小文字は **保持** ("Company" を "company" に下げない)
    - 生成後は不変

    ``match_key`` プロパティは **持たない**。β の canonical_type 集計と
    Stage 1 raw_type を直接合流させない設計を型で表明する。Stage 1 集計時は
    ``raw_type.root`` をそのまま group_by キーに使う。
    """

    model_config = ConfigDict(frozen=True)

    @field_validator("root", mode="before")
    @classmethod
    def _validate(cls, v: Any) -> str:
        if not isinstance(v, str):
            msg = f"Expected str, got {type(v).__name__}"
            raise ValueError(msg)
        normalized = unicodedata.normalize("NFKC", v)
        normalized = _WHITESPACE_RUN.sub(" ", normalized).strip()
        if not normalized:
            msg = "EntityRawType must not be empty"
            raise ValueError(msg)
        if len(normalized) > _RAW_TYPE_MAX_LENGTH:
            msg = (
                f"EntityRawType must be at most {_RAW_TYPE_MAX_LENGTH} chars, "
                f"got {len(normalized)}"
            )
            raise ValueError(msg)
        return normalized

    def __str__(self) -> str:
        return self.root

    def __repr__(self) -> str:
        return f"EntityRawType({self.root!r})"
