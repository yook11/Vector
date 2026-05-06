from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.collection.domain.value_objects.source import SourceName
from app.models.base import Base
from app.shared.value_objects.safe_url import SafeUrl

if TYPE_CHECKING:
    from app.models.fetch_log import FetchLog


class SourceType(StrEnum):
    RSS = "rss"
    API = "api"
    HTML = "html"


class NewsSource(Base):
    __tablename__ = "news_sources"
    __table_args__ = (
        UniqueConstraint("name", name="uq_news_sources_name"),
        CheckConstraint(
            "name != ''",
            name="ck_news_sources_name_not_empty",
        ),
        CheckConstraint(
            "source_type IN ('rss', 'api', 'html')",
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
    name: Mapped[SourceName] = mapped_column()
    source_type: Mapped[SourceType] = mapped_column(String(20))
    site_url: Mapped[SafeUrl] = mapped_column()
    endpoint_url: Mapped[SafeUrl] = mapped_column(unique=True)
    is_active: Mapped[bool] = mapped_column(server_default=sa.true())
    attribution_label: Mapped[str | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # リレーション
    fetch_logs: Mapped[list[FetchLog]] = relationship(back_populates="source")

    # ドメインメソッド
    def activate(self) -> None:
        """このニュースソースを有効化する。"""
        self.is_active = True
        self.updated_at = datetime.now(UTC)

    def deactivate(self) -> None:
        """このニュースソースを無効化する。"""
        self.is_active = False
        self.updated_at = datetime.now(UTC)
