from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.analyzed_article_record import AnalyzedArticleRecord


class WatchlistEntry(Base):
    __tablename__ = "watchlist_entries"

    user_id: Mapped[uuid_mod.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("auth.user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    article_analysis_id: Mapped[int] = mapped_column(
        ForeignKey("analyzed_articles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # リレーション
    # PR1 では column rename を避け、PR2 で analyzed_article_id に寄せる。
    analyzed_article: Mapped[AnalyzedArticleRecord] = relationship(
        back_populates="watchlist_entries"
    )
