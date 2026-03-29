from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.news_source import NewsSource


class FetchStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class FetchLog(Base):
    __tablename__ = "fetch_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("news_sources.id", ondelete="CASCADE"),
        index=True,
    )
    status: Mapped[FetchStatus] = mapped_column(String(20))
    articles_count: Mapped[int] = mapped_column(server_default=sa.text("0"))
    error_message: Mapped[str | None] = mapped_column(Text())
    duration_ms: Mapped[int | None]
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships (same Base — OK)
    source: Mapped[NewsSource] = relationship(back_populates="fetch_logs")
