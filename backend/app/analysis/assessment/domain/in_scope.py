"""InScopeAssessment アグリゲート — Stage 4 で in-scope と判定された評価結果。

``InScopeAssessment`` はシステムに記録された評価結果 Entity。identity (id) と
記録時刻 (analyzed_at) を持ち、Stage 5 (embedding) や FE 出口
(`/api/v1/articles/{id}` / watchlist articleId) が参照する型。

Topic は 2026-04 の決定（memory: project_topic_filter_decision.md）により
表示専用属性に降格した。InScopeAssessment Aggregate は以下を不可分な単位として保証する:
- identity (id / extraction_id)
- translated_title / summary / investor_take
- topic (TopicName VO、正規化済み自由記述ラベル)
- category_id (第一級フィルタ軸、categories.id への FK)
- ai_model / analyzed_at

Topic を別 Aggregate に切り出す将来計画はない
（feedback_aggregate_over_individual_vo.md: 保証はアグリゲート単位）。

AI 境界型 ``InScope`` の sanitize / 長さ上限は ``assessor/schema.py`` 側で
保証され、Stage 3 由来の ``translated_title`` / ``summary`` は
``ExtractionResult`` (BC 境界) で normalize 済。Repository.save が AI 境界型を
そのまま受け取り Entity を返すため、Service 内での詰め替え (旧 Draft) は
不要になっている (feedback_bc_boundary_guarantees_downstream)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.analysis.domain.value_objects.topic import TopicName


@dataclass(frozen=True, slots=True)
class InScopeAssessment:
    """システムに記録された in-scope 評価結果 Entity。

    Stage 4 in-scope 判定確定後の状態。``extraction_id`` を通じて Stage 3 に遡れる。
    ``translated_title`` / ``summary`` は Stage 4 確定時点の Extraction からの
    スナップショット — Extraction が後に再実行されても Assessment 側は更新されない
    (in-scope assessments は自己完結した提示物として扱う)。

    embedding 関連は Stage 5 の成果物であり、この Entity に含めない。

    Topic は表示専用属性として VO で同居する。``category_id: int`` は第一級
    フィルタ軸で、Aggregate 境界を ID で跨ぐ DDD 原則に従い ID 参照のまま持つ。

    ``id`` は FE 公開 API (``/api/v1/articles/{id}``) と watchlist の
    ``articleId`` キーを兼ねており、ORM ``InScopeAssessment.id`` と同一性を維持する。

    Invariants:
    - id / extraction_id / category_id は正の整数
    - translated_title / summary / investor_take / ai_model は非空
    - analyzed_at は記録時刻

    ``__post_init__`` の検査は DB FK NOT NULL + RESTRICT で構造的に保証される
    が、DB を直接編集で壊された場合の検知用に残す（防御的）。
    """

    id: int
    extraction_id: int
    translated_title: str
    summary: str
    topic: TopicName
    category_id: int
    investor_take: str
    ai_model: str
    analyzed_at: datetime

    def __post_init__(self) -> None:
        if not self.translated_title:
            raise ValueError("InScopeAssessment.translated_title must be non-empty")
        if not self.summary:
            raise ValueError("InScopeAssessment.summary must be non-empty")
        if not self.investor_take:
            raise ValueError("InScopeAssessment.investor_take must be non-empty")
        if not self.ai_model:
            raise ValueError("InScopeAssessment.ai_model must be non-empty")
        if self.id <= 0:
            raise ValueError("InScopeAssessment.id must be positive")
        if self.extraction_id <= 0:
            raise ValueError("InScopeAssessment.extraction_id must be positive")
        if self.category_id <= 0:
            raise ValueError("InScopeAssessment.category_id must be positive")
