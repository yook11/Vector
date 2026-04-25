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


__all__ = ["ArticleAnalysis"]


class ArticleAnalysis(Base):
    """Stage 2 で Classified と判定された extraction の分析結果。

    translated_title / summary は extractions から複製保持し、analysis 単独で
    「ユーザーに見せる分析結果」として自己完結する。

    Topic は 2026-04 の決定で表示専用属性に降格し、独立テーブルから当行の
    自由記述カラム（TopicName VO 列）に移動した。Category は第一級フィルタ軸
    として直接 FK を持つ。
    """

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
    extraction: Mapped[ArticleExtraction] = relationship(back_populates="analysis")
    watchlist_entries: Mapped[list[WatchlistEntry]] = relationship(
        back_populates="article_analysis"
    )
