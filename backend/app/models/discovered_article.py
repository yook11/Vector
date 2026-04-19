from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.safe_url import SafeUrl
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article import Article
    from app.models.news_source import NewsSource


class DiscoveredArticle(Base):
    """RSS フィードで発見した記事の記録。"""

    __tablename__ = "discovered_articles"
    __table_args__ = (
        UniqueConstraint("original_url", name="uq_discovered_articles_original_url"),
        CheckConstraint(
            "original_url ~ '^https?://.+'",
            name="ck_discovered_articles_url_scheme",
        ),
        CheckConstraint(
            "original_title != ''",
            name="ck_discovered_articles_title_not_empty",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    news_source_id: Mapped[int] = mapped_column(
        ForeignKey("news_sources.id", ondelete="RESTRICT"),
    )
    original_url: Mapped[SafeUrl] = mapped_column()
    original_title: Mapped[str] = mapped_column(String(500))
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # リレーション
    article: Mapped[Article | None] = relationship(
        back_populates="discovered_article", uselist=False
    )
    news_source: Mapped[NewsSource] = relationship(back_populates="discovered_articles")
