from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
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
    from app.models.article_extraction import ArticleExtraction
    from app.models.extraction_noise import ExtractionNoise
    from app.models.news_source import NewsSource


class Article(Base):
    """分析対象の記事。行が存在する = 分析可能。"""

    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("source_url", name="uq_articles_source_url"),
        UniqueConstraint("article_url_id", name="uq_articles_article_url_id"),
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
    # 新経路の article_urls への外部キー。PR2.5 系の cutover で全 article 行が
    # この経路を持つ前提だが、後続 PR で NOT NULL 昇格を切り出すため当面 nullable。
    article_url_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("article_urls.id", ondelete="RESTRICT"),
        nullable=True,
    )
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
    extraction: Mapped[ArticleExtraction | None] = relationship(
        back_populates="article", uselist=False
    )
    extraction_noise: Mapped[ExtractionNoise | None] = relationship(
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
