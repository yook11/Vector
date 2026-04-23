"""Stage 1 抽出 AI レスポンスの Pydantic スキーマ。

ExtractionResponse は Gemini SDK の ``response_schema`` に渡され、
受信時に構造・不変条件（非空・エンティティ重複なし）を保証する境界型。
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.analysis.domain.value_objects.entity import EntityName, EntityType


class EntityResponse(BaseModel):
    """抽出されたエンティティ 1 件。"""

    model_config = ConfigDict(frozen=True)

    name: EntityName
    type: EntityType


class ExtractionResponse(BaseModel):
    """Stage 1 抽出の構造化レスポンス。

    Invariants:
    - title_ja, summary_ja は非空
    - entities は (name.casefold(), type) で重複排除済み
    """

    model_config = ConfigDict(frozen=True)

    title_ja: str = Field(min_length=1)
    summary_ja: str = Field(min_length=1)
    entities: list[EntityResponse]

    @model_validator(mode="after")
    def _dedupe_entities(self) -> Self:
        seen: set[tuple[str, str]] = set()
        unique: list[EntityResponse] = []
        for e in self.entities:
            key = (e.name.root.casefold(), e.type.root)
            if key in seen:
                continue
            seen.add(key)
            unique.append(e)
        # frozen=True のため object.__setattr__ で直接書き換える
        object.__setattr__(self, "entities", unique)
        return self
