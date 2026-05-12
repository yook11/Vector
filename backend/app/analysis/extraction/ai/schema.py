"""Gemini SDK structured response 契約型 — Stage 3。

``response_schema=GeminiExtractionResponse`` で Gemini に flat schema
(``{relevance, title_ja, summary_ja, entities}``) を要求する SDK 契約型。

ドメイン層は ``Signal`` / ``Noise`` union (``ExtractionResult``) を使うが、
構造化出力で discriminated union を AI に要求すると精度が落ちる + Gemini SDK
の ``response_schema`` の制約により、AI 境界では flat 形式を維持する。
``ai/parse.py::parse_extraction`` が ``relevance`` 値を見て domain 型に振り分ける。

設計詳細:
- VERSION hash の連続性のため、フィールド順 / Pydantic config / validator /
  docstring を旧 ``ExtractionResult`` (Pydantic class、本 PR で union alias に転換)
  と bit-identical に保つ。``GeminiExtractionResponse.model_json_schema()`` が
  ``compute_call_signature`` の入力。schema が変わると ``VERSION`` が変わり
  audit 連続性が失われるため、bit-identical 検証は
  ``tests/analysis/extraction/test_prompt_version.py`` で固定する。
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.analysis.extraction.domain.entity import ExtractedEntity
from app.utils.sanitize import normalize_text


class GeminiExtractionResponse(BaseModel):
    """AI が記事を分析した結果として期待するもの。

    Gemini の ``response_schema`` として使用されるため、フィールド名は
    AI プロンプトの規約に合わせて ``title_ja`` / ``summary_ja`` とする。

    ``relevance`` は Stage 1 signal/noise フィルタの判定結果。``"signal"``
    なら下流 (Stage 4 assessment) へ進み、``"noise"`` なら
    ``extraction_noises`` テーブルに記録して chain しない。

    Invariants (validators で構造的に保証):
    - ``relevance``: ``"signal"`` または ``"noise"``
    - ``title_ja`` / ``summary_ja``: HTML タグ除去 + NFKC + 制御文字除去後に非空
    - ``entities``: ``(surface.match_key, raw_type.root)`` で重複なし
    - frozen: 生成後は不変

    BC 境界として下流 (Stage 4 Assessment) に「HTML 抜き、NFKC 済、制御文字無し、
    非空」を保証する責務を持つ。下流ステージで再 sanitize しない設計の前提
    (feedback_bc_boundary_guarantees_downstream)。
    """

    # ``title="ExtractionResult"`` は VERSION hash の連続性のため
    # (``model_json_schema()`` の ``title`` 値がクラス名に依存するため、PR1-a 前の
    # 旧 ``ExtractionResult`` Pydantic class が ``RESPONSE_SCHEMA`` だった頃の
    # hash 入力と bit-identical に保つ必要がある)。試験で固定済
    # (``tests/analysis/extraction/ai/test_gemini_prompt.py``)。
    model_config = ConfigDict(frozen=True, title="ExtractionResult")

    relevance: Literal["signal", "noise"]
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
