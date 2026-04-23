from __future__ import annotations

from datetime import datetime
from enum import StrEnum
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

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article_extraction import ArticleExtraction
    from app.models.topic import Topic
    from app.models.watchlist_entry import WatchlistEntry


class ImpactLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ArticleAnalysis(Base):
    """Stage 2 で Classified と判定された extraction の分析結果。

    translated_title / summary は extractions から複製保持し、analysis 単独で
    「ユーザーに見せる分析結果」として自己完結する。
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
            "reasoning != ''",
            name="ck_article_analyses_reasoning_not_empty",
        ),
        # サイドバーの直近 24 時間集計クエリ向けの複合インデックス
        Index(
            "ix_article_analyses_topic_id_analyzed_at",
            "topic_id",
            "analyzed_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_id: Mapped[int] = mapped_column(
        ForeignKey("article_extractions.id", ondelete="CASCADE"),
    )
    translated_title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text())
    impact_level: Mapped[ImpactLevel] = mapped_column(String(20))
    reasoning: Mapped[str] = mapped_column(Text())
    ai_model: Mapped[str] = mapped_column(String(100))
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(768))
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="RESTRICT"), index=True
    )

    # リレーション
    extraction: Mapped[ArticleExtraction] = relationship(back_populates="analysis")
    topic: Mapped[Topic] = relationship()
    watchlist_entries: Mapped[list[WatchlistEntry]] = relationship(
        back_populates="article_analysis"
    )

    @classmethod
    def from_classification(
        cls,
        *,
        extraction: ArticleExtraction,
        topic_id: int,
        impact_level: ImpactLevel,
        reasoning: str,
        model_name: str,
    ) -> ArticleAnalysis:
        """Stage 2 の分類結果から分析オブジェクトを構築する。

        translated_title / summary は extraction から複製する（自己完結の保証）。
        """
        return cls(
            extraction_id=extraction.id,
            translated_title=extraction.translated_title,
            summary=extraction.summary,
            topic_id=topic_id,
            impact_level=impact_level,
            reasoning=reasoning,
            ai_model=model_name,
        )
