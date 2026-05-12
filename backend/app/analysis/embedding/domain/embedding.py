"""Embedding ドメイン入力 — Stage 5 で生成された埋め込みベクトルの永続化前型。

``EmbeddingDraft`` のみで構成される単一型ドメインモデル:

- ``EmbeddingDraft`` — AI 境界の ``list[float]`` を ``EmbeddingVector`` VO に
  正規化したドメイン入力。analysis_id / model_name は Service が
  ``Repository.save`` 呼び出し時に解決する。

永続化後の Entity 型は廃止済み (2026-05-12)。Stage 5 は pipeline 終端で
下流に Entity を渡す価値がなく、Repository.save は ``bool`` (保存成否)
だけを返す。読み出し側 (search BC) は ``InScopeAssessment`` ORM カラムを
直接参照するため、Entity 復元の経路は持たない。
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict

from app.analysis.embedding.domain.value_objects import EmbeddingVector


class EmbeddingDraft(BaseModel):
    """埋め込み生成結果のドメイン入力 (AI 出力 → 永続化前の正規化値)。

    AI 境界の ``list[float]`` を ``EmbeddingVector`` VO に正規化した状態。
    analysis_id / model_name はこの段階では未確定で、Service が
    ``Repository.save`` 呼び出し時に注入する。

    Invariants:
    - ``vector``: ``EmbeddingVector`` (768 dim + 有限性 + サニティ範囲)
    - frozen: 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    vector: EmbeddingVector

    @classmethod
    def from_inference(cls, *, vector: list[float]) -> Self:
        """AI 境界の ``list[float]`` を Draft に正規化する。

        embedder の戻り値をそのまま受け取り、``EmbeddingVector`` で
        次元・有限性・サニティ範囲を構造的に検証する純粋変換。
        """
        return cls(vector=EmbeddingVector(root=tuple(vector)))
