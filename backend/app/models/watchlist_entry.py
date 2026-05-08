from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.in_scope_assessment import InScopeAssessment


class WatchlistEntry(Base):
    __tablename__ = "watchlist_entries"

    user_id: Mapped[uuid_mod.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("auth.user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    article_analysis_id: Mapped[int] = mapped_column(
        ForeignKey("article_analyses.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # リレーション
    # PR3.5-d.0: ORM クラス名は InScopeAssessment に rename 済。属性名 / カラム名
    # (article_analysis_id) は API/DB 互換のため据え置き。
    in_scope_assessment: Mapped[InScopeAssessment] = relationship(
        back_populates="watchlist_entries"
    )
