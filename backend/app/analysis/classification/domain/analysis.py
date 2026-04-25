"""Analysis アグリゲート — Stage 2 で Classified 判定された分析結果。

2 つの型で Stage 2 Classified の概念を表す:

- ``AnalysisDraft`` — AI 境界型 ``Classified`` を sanitize / 正規化した
  ドメイン入力。永続化前の状態で、extraction_id / category_id / ai_model など
  Service が解決するフィールドは含まない。
- ``Analysis`` — システムに記録された分析結果 Entity。identity (id) と
  記録時刻 (analyzed_at) を持ち、Stage 3 (embedding) や FE 出口
  (`/api/v1/articles/{id}` / watchlist articleId) が参照する型。

Topic は 2026-04 の決定（memory: project_topic_filter_decision.md）により
表示専用属性に降格した。Analysis Aggregate は以下を不可分な単位として保証する:
- identity (id / extraction_id)
- translated_title / summary / investor_take
- topic (TopicName VO、正規化済み自由記述ラベル)
- category_id (第一級フィルタ軸、categories.id への FK)
- ai_model / analyzed_at

Topic を別 Aggregate に切り出す将来計画はない
（feedback_aggregate_over_individual_vo.md: 保証はアグリゲート単位）。

変換は ``AnalysisDraft.from_classified`` (AI 境界 → Draft) と
``Analysis.from_draft`` (Draft + identity → Entity)、Repository._to_domain
(ORM → Entity) が担う。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.analysis.classifier.schema import Classified
from app.analysis.domain.value_objects.topic import TopicName
from app.utils.sanitize import normalize_text


class AnalysisDraft(BaseModel):
    """Stage 2 で Classified 判定された分析結果のドメイン入力。

    AI 境界型 ``Classified`` を受けて sanitize + 正規化した後の状態。
    extraction (identity 付き Entity) と組み合わせて ``Analysis`` Entity に
    昇格する。category_id はこの段階では未確定で、Service が
    ``get_category_id_by_slug`` で解決してから Entity に詰める。

    Invariants (validators で構造的に保証):
    - ``translated_title``: sanitize 後 1-500 文字
    - ``summary``: sanitize 後 1-4000 文字
    - ``investor_take``: sanitize 後 1-2000 文字 (Prompt Injection DoS 対策で上限)
    - ``topic_name``: ``TopicName`` VO で正規化済み
    - frozen: 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    translated_title: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=4000)
    topic_name: TopicName
    investor_take: str = Field(min_length=1, max_length=2000)

    @field_validator("translated_title", "summary", "investor_take", mode="before")
    @classmethod
    def _sanitize(cls, v: Any) -> Any:
        if isinstance(v, str):
            return normalize_text(v) or ""
        return v

    @field_validator("translated_title", "summary", "investor_take")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("must be non-empty after sanitization")
        return v

    @classmethod
    def from_classified(
        cls,
        classified: Classified,
        *,
        translated_title: str,
        summary: str,
    ) -> Self:
        """AI 境界型 ``Classified`` と Stage 1 から複製する値を受けて Draft を構築する。

        ``translated_title`` / ``summary`` は ``Extraction`` のスカラ 2 つを
        引数に取る (Extraction 全体ではなく)。Extraction の構造変更が Draft 側に
        波及しないよう最小結合に保つための意図。
        """
        return cls(
            translated_title=translated_title,
            summary=summary,
            topic_name=classified.topic,
            investor_take=classified.investor_take,
        )


@dataclass(frozen=True, slots=True)
class Analysis:
    """システムに記録された分析結果 Entity。

    Stage 2 Classified 確定後の状態。``extraction_id`` を通じて Stage 1 に遡れる。
    ``translated_title`` / ``summary`` は Stage 2 確定時点の Extraction からの
    スナップショット — Extraction が後に再実行されても Analysis 側は更新されない
    (analyses は自己完結した提示物として扱う)。

    embedding 関連は Stage 3 の成果物であり、この Entity に含めない。

    Topic は表示専用属性として VO で同居する。``category_id: int`` は第一級
    フィルタ軸で、Aggregate 境界を ID で跨ぐ DDD 原則に従い ID 参照のまま持つ。

    ``id`` は FE 公開 API (``/api/v1/articles/{id}``) と watchlist の
    ``articleId`` キーを兼ねており、``ArticleAnalysis.id`` と同一性を維持する。

    Invariants:
    - id / extraction_id / category_id は正の整数
    - translated_title / summary / investor_take / ai_model は非空
    - analyzed_at は記録時刻

    ``__post_init__`` の検査は DB FK NOT NULL + RESTRICT で構造的に保証される
    が、DB を直接編集で壊された場合の検知用に残す（防御的）。通常は Draft
    バリデータが先に弾く。
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
            raise ValueError("Analysis.translated_title must be non-empty")
        if not self.summary:
            raise ValueError("Analysis.summary must be non-empty")
        if not self.investor_take:
            raise ValueError("Analysis.investor_take must be non-empty")
        if not self.ai_model:
            raise ValueError("Analysis.ai_model must be non-empty")
        if self.id <= 0:
            raise ValueError("Analysis.id must be positive")
        if self.extraction_id <= 0:
            raise ValueError("Analysis.extraction_id must be positive")
        if self.category_id <= 0:
            raise ValueError("Analysis.category_id must be positive")

    @classmethod
    def from_draft(
        cls,
        draft: AnalysisDraft,
        *,
        id: int,
        extraction_id: int,
        category_id: int,
        ai_model: str,
        analyzed_at: datetime,
    ) -> Self:
        """Draft に identity を合成して永続化済み Entity を組み立てる。

        Repository.save が返した ``id`` / ``analyzed_at``、Service が
        ``get_category_id_by_slug`` で解決した ``category_id``、classifier から
        受け取った ``ai_model`` を合わせて「記録された Entity」を構成する
        ドメインファクトリ。
        """
        return cls(
            id=id,
            extraction_id=extraction_id,
            translated_title=draft.translated_title,
            summary=draft.summary,
            topic=draft.topic_name,
            category_id=category_id,
            investor_take=draft.investor_take,
            ai_model=ai_model,
            analyzed_at=analyzed_at,
        )
