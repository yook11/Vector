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
        ForeignKey("in_scope_assessments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # リレーション
    # 属性名 / カラム名 (article_analysis_id) は据え置きで確定。
    # ユーザ視点で「ウォッチした分析記事」を表す概念名として保持する判断
    # (PR3.5-d.2 調査記録、specs/stage4-assessment-rename.md 参照)。
    in_scope_assessment: Mapped[InScopeAssessment] = relationship(
        back_populates="watchlist_entries"
    )
