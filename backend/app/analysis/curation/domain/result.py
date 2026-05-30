"""Curation BC のドメイン結果型 (AI 出力 DTO)。

AI 境界では引き続きフラット形式 (``{relevance, title_ja, summary_ja}``)
を AI に要求する (構造化出力で discriminated union を要求すると AI 精度が
落ちる + Gemini SDK structured response の制約)。Gemini SDK 契約型は
``ai/schema.py::GeminiCurationResponse`` として分離し、``ai/parse.py::
parse_curation`` が ``relevance`` 値を見て ``Signal`` / ``Noise`` に振り分ける。

永続化結果 (``article_curations`` / ``curation_noises`` の 1 行) は
Repository が ``int`` id として返し、Domain Entity 化はしない。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from app.shared.text import normalize_text


class Signal(BaseModel):
    """signal 判定された AI 分析結果 — 下流 Stage 4 (assessment) に chain する。

    Invariants (validators で構造的に保証):
    - ``title_ja`` / ``summary_ja``: HTML タグ除去 + NFKC + 制御文字除去後に非空
    - frozen: 生成後は不変

    BC 境界として下流 (Stage 4 Assessment) に「HTML 抜き、NFKC 済、制御文字無し、
    非空」を保証する責務を持つ。下流ステージで再 sanitize しない設計の前提
    (feedback_bc_boundary_guarantees_downstream)。
    """

    model_config = ConfigDict(frozen=True)

    title_ja: str
    summary_ja: str

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


class Noise(BaseModel):
    """noise 判定された AI 分析結果 — ``curation_noises`` に記録し chain しない。

    ``Signal`` と shape は同一だが、型として分けることで Service の
    ``match call: case CurationCall(result=Signal()): | case
    CurationCall(result=Noise()):`` で型ディスパッチを効かせる
    (Stage 4 ``InScope`` / ``OutOfScope`` と対称)。

    Invariants / BC 境界責務は ``Signal`` と同じ。
    """

    model_config = ConfigDict(frozen=True)

    title_ja: str
    summary_ja: str

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


CurationResult = Signal | Noise
"""Stage 3 (Curation) の判定結果。Service はこの union を ``match`` で分岐する。"""
