"""OutOfScopeAssessment アグリゲート — Stage 4 で out-of-scope と判定された記録。

2 つの型で out-of-scope の概念を表す:

- ``OutOfScopeAssessmentDraft`` — AI 境界型 ``OutOfScope`` を sanitize した
  ドメイン入力。investor_take のみを持ち、Stage 3 データは複製しない
  (ユーザーには見せないため)。
- ``OutOfScopeAssessment`` — 対象範囲外判定の記録 Entity。identity (id) と
  記録時刻 (rejected_at) を持つ。

``OutOfScopeAssessment`` は ``InScopeAssessment`` と別アグリゲートとして扱う:
- ``InScopeAssessment`` は「ユーザーに見せる確定評価結果」
- ``OutOfScopeAssessment`` は「監査・トレース用の対象外記録」
役割と寿命管理が違うため、実装の見た目が似ていても別型に分ける。

このアグリゲートは認証された admin ロールまたは内部 observability のみ
公開を許容する。REST API 経由で一般ユーザーに返してはならない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.analysis.classifier.schema import OutOfScope
from app.utils.sanitize import normalize_text


class OutOfScopeAssessmentDraft(BaseModel):
    """Stage 4 で out-of-scope と判定された記録のドメイン入力。

    AI 境界型 ``OutOfScope`` を受けて sanitize した後の状態。investor_take のみを
    持ち、Stage 3 のデータは複製しない (ユーザーには見せないため)。

    Invariants:
    - ``investor_take``: sanitize 後 1-2000 文字 (Prompt Injection DoS 対策で上限)
    - frozen: 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    investor_take: str = Field(min_length=1, max_length=2000)

    @field_validator("investor_take", mode="before")
    @classmethod
    def _sanitize(cls, v: Any) -> Any:
        if isinstance(v, str):
            return normalize_text(v) or ""
        return v

    @field_validator("investor_take")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("investor_take must be non-empty after sanitization")
        return v

    @classmethod
    def from_out_of_scope(cls, out_of_scope: OutOfScope) -> Self:
        """AI 境界型 ``OutOfScope`` を受けて Draft を構築する。"""
        return cls(investor_take=out_of_scope.investor_take)


@dataclass(frozen=True, slots=True)
class OutOfScopeAssessment:
    """対象範囲外判定の記録 Entity。

    identity は実質 ``extraction_id`` (UNIQUE) — ``id`` は DB 都合の採番値。
    将来「extraction ごとに複数 out-of-scope 履歴」が必要になったら ``id`` が
    独立した意味を持つようになる。

    Invariants:
    - id / extraction_id は正の整数
    - investor_take / ai_model は非空
    - rejected_at は記録時刻

    ``__post_init__`` の検査は DB CHECK + FK NOT NULL と一致する。通常は
    Draft バリデータが先に弾くが、DB が壊れた場合の検知用。
    """

    id: int
    extraction_id: int
    investor_take: str
    ai_model: str
    rejected_at: datetime

    def __post_init__(self) -> None:
        if not self.investor_take:
            raise ValueError("OutOfScopeAssessment.investor_take must be non-empty")
        if not self.ai_model:
            raise ValueError("OutOfScopeAssessment.ai_model must be non-empty")
        if self.id <= 0:
            raise ValueError("OutOfScopeAssessment.id must be positive")
        if self.extraction_id <= 0:
            raise ValueError("OutOfScopeAssessment.extraction_id must be positive")
