from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article_keyword import ArticleKeyword
    from app.models.news_article import NewsArticle
    from app.models.watchlist_entry import WatchlistEntry


class ImpactLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ArticleAnalysis(Base):
    __tablename__ = "article_analyses"
    __table_args__ = (
        UniqueConstraint("news_article_id", name="uq_article_analyses_news_article_id"),
        CheckConstraint(
            "translated_title != ''",
            name="ck_article_analyses_translated_title_not_empty",
        ),
        CheckConstraint(
            "summary != ''",
            name="ck_article_analyses_summary_not_empty",
        ),
        CheckConstraint(
            "reasoning != ''",
            name="ck_article_analyses_reasoning_not_empty",
        ),
        CheckConstraint(
            "ai_model != ''",
            name="ck_article_analyses_ai_model_not_empty",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    news_article_id: Mapped[int] = mapped_column(
        ForeignKey("news_articles.id", ondelete="CASCADE"),
    )
    translated_title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text())
    impact_level: Mapped[ImpactLevel] = mapped_column(String(20))
    reasoning: Mapped[str] = mapped_column(Text())
    ai_model: Mapped[str] = mapped_column(String(100))
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(768))
    embedding_model: Mapped[str | None] = mapped_column(String(100))

    # リレーション
    news_article: Mapped[NewsArticle] = relationship(back_populates="article_analysis")
    article_keywords: Mapped[list[ArticleKeyword]] = relationship(
        back_populates="article_analysis"
    )
    watchlist_entries: Mapped[list[WatchlistEntry]] = relationship(
        back_populates="article_analysis"
    )
