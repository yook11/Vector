"""Extraction BC のドメイン結果型 (AI 出力 DTO)。

2 つの型で Stage 3 の AI 出力を表す:

- ``Signal`` / ``Noise`` — AI が記事を分析した結果として産出する domain 結果型。
  ``ExtractionResult = Signal | Noise`` の union alias を経由して Service /
  Repository が ``match`` で型ディスパッチする (Stage 4 ``InScope`` /
  ``OutOfScope`` と対称)。

AI 境界では引き続きフラット形式 (``{relevance, title_ja, summary_ja, entities}``)
を AI に要求する (構造化出力で discriminated union を要求すると AI 精度が
落ちる + Gemini SDK structured response の制約)。Gemini SDK 契約型は
``ai/schema.py::GeminiExtractionResponse`` として分離し、``ai/parse.py::
parse_extraction`` が ``relevance`` 値を見て ``Signal`` / ``Noise`` に振り分ける。

永続化結果 (``article_extractions`` / ``extraction_noises`` の 1 行) は
Repository が ``int`` id として返し、Domain Entity 化はしない (Stage 4 / Stage 5
と対称な勝者 SSoT パターン、``feedback_outcome_purification``)。
"""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.analysis.extraction.domain.entity import ExtractedEntity
from app.utils.sanitize import normalize_text


class Signal(BaseModel):
    """signal 判定された AI 分析結果 — 下流 Stage 4 (assessment) に chain する。

    Invariants (validators で構造的に保証):
    - ``title_ja`` / ``summary_ja``: HTML タグ除去 + NFKC + 制御文字除去後に非空
    - ``entities``: ``(surface.match_key, raw_type.root)`` で重複なし
    - frozen: 生成後は不変

    BC 境界として下流 (Stage 4 Assessment) に「HTML 抜き、NFKC 済、制御文字無し、
    非空」を保証する責務を持つ。下流ステージで再 sanitize しない設計の前提
    (feedback_bc_boundary_guarantees_downstream)。
    """

    model_config = ConfigDict(frozen=True)

    title_ja: str
    summary_ja: str
    entities: list[ExtractedEntity]

    @field_validator("title_ja", "summary_ja", mode="before")
    @classmethod
    def _sanitize(cls, v: Any) -> Any:
        """HTML タグ除去 + NFKC + 制御文字除去 + 前後空白トリム。"""
        if isinstance(v, str):
            return normalize_text(v) or ""
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
        unique: list[ExtractedEntity] = []
        for e in self.entities:
            key = e.dedup_key()
            if key in seen:
                continue
            seen.add(key)
            unique.append(e)
        # frozen=True のため object.__setattr__ で直接書き換える
        object.__setattr__(self, "entities", unique)
        return self


class Noise(BaseModel):
    """noise 判定された AI 分析結果 — ``extraction_noises`` に記録し chain しない。

    ``Signal`` と shape は同一だが、型として分けることで Service の
    ``match call: case ExtractionCall(result=Signal()): | case
    ExtractionCall(result=Noise()):`` で型ディスパッチを効かせる
    (Stage 4 ``InScope`` / ``OutOfScope`` と対称)。

    Invariants / BC 境界責務は ``Signal`` と同じ。
    """

    model_config = ConfigDict(frozen=True)

    title_ja: str
    summary_ja: str
    entities: list[ExtractedEntity]

    @field_validator("title_ja", "summary_ja", mode="before")
    @classmethod
    def _sanitize(cls, v: Any) -> Any:
        if isinstance(v, str):
            return normalize_text(v) or ""
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
        unique: list[ExtractedEntity] = []
        for e in self.entities:
            key = e.dedup_key()
            if key in seen:
                continue
            seen.add(key)
            unique.append(e)
        object.__setattr__(self, "entities", unique)
        return self


ExtractionResult = Signal | Noise
"""Stage 3 (Extraction) の判定結果。Service はこの union を受け取り
``match`` で ``Signal`` / ``Noise`` に型ディスパッチする (Stage 4
``AssessmentResult = InScope | OutOfScope`` と対称)。AI 境界の Gemini SDK
契約型は ``ai/schema.py::GeminiExtractionResponse`` を参照。
"""
