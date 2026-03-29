from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.safe_url import SafeUrl
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article_analysis import ArticleAnalysis
    from app.models.article_keyword import ArticleKeyword
    from app.models.news_source import NewsSource
    from app.models.watchlist_entry import WatchlistEntry


class NewsArticle(Base):
    __tablename__ = "news_articles"
    __table_args__ = (
        UniqueConstraint("original_url", name="uq_news_articles_original_url"),
        CheckConstraint(
            "original_url ~ '^https?://.+'",
            name="ck_news_articles_url_scheme",
        ),
        CheckConstraint(
            "original_title != ''",
            name="ck_news_articles_title_not_empty",
        ),
        Index("idx_news_published", "published_at", postgresql_using="btree"),
        Index(
            "idx_content_fetch_pending",
            "skip_content_fetch",
            postgresql_where=text(
                "original_content IS NULL AND skip_content_fetch = false"
            ),
        ),
        Index(
            "idx_news_source_published",
            "news_source_id",
            text("published_at DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    original_title: Mapped[str] = mapped_column(String(500))
    original_url: Mapped[SafeUrl] = mapped_column()
    original_content: Mapped[str | None] = mapped_column(Text())
    original_description: Mapped[str | None] = mapped_column(String(2000))
    news_source_id: Mapped[int] = mapped_column(
        ForeignKey("news_sources.id", ondelete="RESTRICT"),
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    skip_content_fetch: Mapped[bool] = mapped_column(server_default=sa.false())

    # Relationships
    article_analysis: Mapped[ArticleAnalysis | None] = relationship(
        back_populates="news_article", uselist=False
    )
    news_source: Mapped[NewsSource] = relationship(back_populates="articles")
    article_keywords: Mapped[list[ArticleKeyword]] = relationship(
        back_populates="news_article"
    )
    watchlist_entries: Mapped[list[WatchlistEntry]] = relationship(
        back_populates="news_article"
    )
