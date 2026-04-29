"""Extraction BC のドメイン概念。

2 つの型で Stage C の概念を表す:

- ``ExtractionResult`` — AI が記事を分析した結果として期待するもの。
  Gemini の ``response_schema`` として渡す契約型と、ドメイン不変条件
  (サニタイズ済み・重複排除済み・非空) を一本化する。型が通過すれば
  「妥当な分析結果」が保証されるので、下流で isvalid 的な分岐が消える。

- ``Extraction`` — システムに記録された分析結果 Entity。identity (id) と
  記録時刻 (extracted_at) を持ち、classification 以降の処理が継続的に
  扱う概念。

変換は Repository.save が直接 ``Extraction`` を返す (Pattern A' Phase 3)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.analysis.extraction.domain.entity import Entity
from app.utils.sanitize import strip_html_tags


class ExtractionResult(BaseModel):
    """AI が記事を分析した結果として期待するもの。

    Gemini の ``response_schema`` として使用されるため、フィールド名は
    AI プロンプトの規約に合わせて ``title_ja`` / ``summary_ja`` とする。

    Invariants (validators で構造的に保証):
    - ``title_ja`` / ``summary_ja``: HTML タグ除去後に非空
    - ``entities``: ``(name.casefold(), type)`` で重複なし
    - frozen: 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    title_ja: str
    summary_ja: str
    entities: list[Entity]

    @field_validator("title_ja", "summary_ja", mode="before")
    @classmethod
    def _sanitize(cls, v: Any) -> Any:
        """HTML タグを除去し、残った空白でもトリムする。"""
        if isinstance(v, str):
            return strip_html_tags(v) or ""
        return v

    @field_validator("title_ja", "summary_ja")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("must be non-empty after sanitization")
        return v

    @model_validator(mode="after")
    def _dedupe_entities(self) -> Self:
        seen: set[tuple[str, str]] = set()
        unique: list[Entity] = []
        for e in self.entities:
            key = e.dedup_key()
            if key in seen:
                continue
            seen.add(key)
            unique.append(e)
        # frozen=True のため object.__setattr__ で直接書き換える
        object.__setattr__(self, "entities", unique)
        return self


@dataclass(frozen=True, slots=True)
class Extraction:
    """システムに記録された分析結果 Entity。

    identity (id) と記録時刻 (extracted_at) を持ち、FK で参照されうる。
    フィールドはすべて必須 — 永続化前の状態は型レベルで表現しない
    (その概念は ``ExtractionResult`` が担当する)。

    Invariants:
    - id は DB が採番した正の整数
    - translated_title / summary / ai_model は非空 (DB CHECK 制約と一致)
    - entities は ``(name.casefold(), type)`` で重複なし (``ExtractionResult``
      通過時点で保証済みのはずだが、DB 復元時の安全網として __post_init__ で検査)
    """

    id: int
    translated_title: str
    summary: str
    entities: tuple[Entity, ...]
    ai_model: str
    extracted_at: datetime

    def __post_init__(self) -> None:
        if not self.translated_title:
            raise ValueError("Extraction.translated_title must be non-empty")
        if not self.summary:
            raise ValueError("Extraction.summary must be non-empty")
        if not self.ai_model:
            raise ValueError("Extraction.ai_model must be non-empty")
        seen: set[tuple[str, str]] = set()
        for e in self.entities:
            key = e.dedup_key()
            if key in seen:
                raise ValueError(
                    f"Extraction.entities must be deduplicated, duplicated: {key!r}"
                )
            seen.add(key)
