"""Stage 4 (Assessment) で in-scope と判定された extraction の評価結果 (ORM)。

translated_title / summary は extractions から複製保持し、assessment 単独で
「ユーザーに見せる分析結果」として自己完結する。

Topic は 2026-04 の決定で表示専用属性に降格し、独立テーブルから当行の
自由記述カラム（TopicName VO 列）に移動した。Category は第一級フィルタ軸
として直接 FK を持つ。

注: ``__tablename__`` は旧名 ``article_analyses`` のまま据え置き。DB rename
は PR3.5-d.1 の Alembic migration で行う。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.analysis.domain.value_objects.topic import TopicName
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article_extraction import ArticleExtraction
    from app.models.watchlist_entry import WatchlistEntry


__all__ = ["InScopeAssessment"]


class InScopeAssessment(Base):
    """Stage 4 で in-scope と判定された extraction の評価結果 (ORM)。"""

    # PR3.5-d.0: 旧名 "article_analyses" のまま据え置き。PR3.5-d.1 で
    # "in_scope_assessments" に rename 予定。
    __tablename__ = "article_analyses"
    __table_args__ = (
        UniqueConstraint("extraction_id", name="uq_article_analyses_extraction_id"),
        CheckConstraint(
            "translated_title != ''",
            name="ck_article_analyses_translated_title_not_empty",
        ),
        CheckConstraint(
            "summary != ''",
            name="ck_article_analyses_summary_not_empty",
        ),
        CheckConstraint(
            "ai_model != ''",
            name="ck_article_analyses_ai_model_not_empty",
        ),
        CheckConstraint(
            "investor_take != ''",
            name="ck_article_analyses_investor_take_not_empty",
        ),
        CheckConstraint(
            "topic <> ''",
            name="ck_article_analyses_topic_not_empty",
        ),
        # TopicName VO の正規表現と揃える DB 側多層防御。
        CheckConstraint(
            r"topic ~ '^[a-z0-9]+( [a-z0-9]+){0,2}$'",
            name="ck_article_analyses_topic_format",
        ),
        # embedding と embedding_model は片方だけ NULL の状態を許さない。
        # ドメインの「未生成 ⇔ 生成済み」2 状態モデルを DB で構造的に強制する
        # (defense-in-depth として Repository._to_domain でも検知)。
        CheckConstraint(
            "(embedding IS NULL AND embedding_model IS NULL) "
            "OR (embedding IS NOT NULL AND embedding_model IS NOT NULL)",
            name="ck_article_analyses_embedding_consistency",
        ),
        # サイドバーの直近 24 時間集計クエリ向けの複合インデックス。
        # 単独 ix_article_analyses_category_id は作らない（複合の左端でカバー）。
        Index(
            "ix_article_analyses_category_id_analyzed_at",
            "category_id",
            "analyzed_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_id: Mapped[int] = mapped_column(
        ForeignKey("article_extractions.id", ondelete="CASCADE"),
    )
    translated_title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text())
    investor_take: Mapped[str] = mapped_column(Text())
    ai_model: Mapped[str] = mapped_column(String(100))
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(768))
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    # 表示専用の自由記述ラベル。TopicNameType により VO ↔ str を双方向強制。
    topic: Mapped[TopicName] = mapped_column()
    # 第一級フィルタ軸。リレーションは YAGNI で持たない（必要時に
    # selectinload を強制する形で別途追加する）。
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"),
    )

    # リレーション
    extraction: Mapped[ArticleExtraction] = relationship(
        back_populates="in_scope_assessment"
    )
    watchlist_entries: Mapped[list[WatchlistEntry]] = relationship(
        back_populates="in_scope_assessment"
    )
