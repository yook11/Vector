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
from app.models.types import SafeUrlType
from app.shared.value_objects.safe_url import SafeUrl

if TYPE_CHECKING:
    from app.models.article_curation import ArticleCuration
    from app.models.curation_noise import CurationNoise
    from app.models.news_source import NewsSource


class Article(Base):
    """分析対象の記事。行が存在する = 分析可能。"""

    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("source_url", name="uq_articles_source_url"),
        CheckConstraint(
            "original_title != ''",
            name="ck_articles_title_not_empty",
        ),
        CheckConstraint(
            "source_url ~ '^https?://.+'",
            name="ck_articles_source_url_scheme",
        ),
        Index("idx_articles_published", "published_at"),
        Index("ix_articles_source_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("news_sources.id", ondelete="RESTRICT"),
    )
    source_url: Mapped[SafeUrl] = mapped_column(SafeUrlType)
    original_title: Mapped[str] = mapped_column(String(500))
    original_content: Mapped[str] = mapped_column(Text())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # source_id FK 経由の直 relationship。
    news_source: Mapped[NewsSource] = relationship()
    curation: Mapped[ArticleCuration | None] = relationship(
        back_populates="article", uselist=False
    )
    curation_noise: Mapped[CurationNoise | None] = relationship(
        back_populates="article", uselist=False
    )

    @property
    def original_url(self) -> SafeUrl:
        """API レスポンス用の便利プロパティ。

        ``articles.source_url`` は Stage 1 で正規化済の URL (NOT NULL、
        SafeUrl 型)。新経路 / 旧経路を問わず常に値があるため、
        relationship を経由せず直接返す。
        """
        return self.source_url
