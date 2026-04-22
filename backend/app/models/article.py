from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

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
    from app.models.discovered_article import DiscoveredArticle
    from app.models.news_source import NewsSource


class Article(Base):
    """分析対象の記事。行が存在する = 分析可能。"""

    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint(
            "discovered_article_id", name="uq_articles_discovered_article_id"
        ),
        CheckConstraint(
            "original_title != ''",
            name="ck_articles_title_not_empty",
        ),
        Index("idx_articles_published", "published_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    discovered_article_id: Mapped[int] = mapped_column(
        ForeignKey("discovered_articles.id", ondelete="CASCADE"),
    )
    original_title: Mapped[str] = mapped_column(String(500))
    original_content: Mapped[str] = mapped_column(Text())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # リレーション
    discovered_article: Mapped[DiscoveredArticle] = relationship(
        back_populates="article"
    )
    extraction: Mapped[ArticleExtraction | None] = relationship(
        back_populates="article", uselist=False
    )

    @property
    def original_url(self) -> str:
        """API レスポンス用の便利プロパティ。"""
        return str(self.discovered_article.original_url)

    @property
    def news_source(self) -> NewsSource:
        """API レスポンス用の便利プロパティ。"""
        return self.discovered_article.news_source
