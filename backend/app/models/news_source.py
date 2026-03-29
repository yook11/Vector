from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.fetch_log import FetchLog
    from app.models.news_article import NewsArticle


class SourceType(StrEnum):
    RSS = "rss"
    API = "api"


class NewsSource(Base):
    __tablename__ = "news_sources"
    __table_args__ = (
        UniqueConstraint(
            "name", "source_type", name="uq_news_sources_name_source_type"
        ),
        CheckConstraint(
            "source_type IN ('rss', 'api')",
            name="ck_news_sources_source_type",
        ),
        CheckConstraint(
            "site_url ~ '^https?://.+'",
            name="ck_news_sources_site_url_scheme",
        ),
        CheckConstraint(
            "endpoint_url ~ '^https?://.+'",
            name="ck_news_sources_endpoint_url_scheme",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    source_type: Mapped[SourceType] = mapped_column(String(20))
    site_url: Mapped[str] = mapped_column(String(2048))
    endpoint_url: Mapped[str] = mapped_column(String(2048), unique=True)
    is_active: Mapped[bool] = mapped_column(server_default=sa.true())

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    articles: Mapped[list[NewsArticle]] = relationship(back_populates="news_source")
    fetch_logs: Mapped[list[FetchLog]] = relationship(back_populates="source")
