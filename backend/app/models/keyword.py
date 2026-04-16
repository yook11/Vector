from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.keyword import KeywordName
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.article_keyword import ArticleKeyword
    from app.models.category import Category


class KeywordStatus(StrEnum):
    PROVISIONAL = "provisional"
    OFFICIAL = "official"
    BLACKLISTED = "blacklisted"


class Keyword(Base):
    __tablename__ = "keywords"
    __table_args__ = (
        CheckConstraint(
            "(status = 'official' AND approved_at IS NOT NULL) "
            "OR (status != 'official' AND approved_at IS NULL)",
            name="ck_keywords_status_approved_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[KeywordName] = mapped_column(unique=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default=KeywordStatus.PROVISIONAL, nullable=False
    )
    is_ai_generated: Mapped[bool] = mapped_column(default=False)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # リレーション（同一 Base のため OK）
    category: Mapped[Category] = relationship(back_populates="keywords")
    article_keywords: Mapped[list[ArticleKeyword]] = relationship(
        back_populates="keyword"
    )
