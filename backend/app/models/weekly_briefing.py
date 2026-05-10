"""カテゴリ単位の週次 LLM 解説 (briefing) の ORM モデル。

DeepSeek-V4 Pro が生成した ``WeeklyBriefingContent`` を 1 行 1 ブリーフィングとして
保持する。``stories`` は ``BriefingStory`` のリストをそのまま JSONB に格納し、
検索/監査属性 (``headline`` / ``model_name`` / ``input_article_count``) のみ
カラム抽出する (feedback_briefing_design_lessons.md)。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["WeeklyBriefing"]


class WeeklyBriefing(Base):
    """1 カテゴリ × 1 週の LLM 解説。"""

    __tablename__ = "weekly_briefings"
    __table_args__ = (
        UniqueConstraint(
            "week_start_date",
            "category_id",
            name="uq_weekly_briefing",
        ),
        CheckConstraint(
            "input_article_count >= 0",
            name="ck_weekly_briefings_count_non_negative",
        ),
        # 「カテゴリの最新 briefing」取得を高速化する DESC index。
        Index(
            "ix_weekly_briefings_category_week",
            "category_id",
            text("week_start_date DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    week_start_date: Mapped[date] = mapped_column()
    category_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("categories.id"),
    )
    headline: Mapped[str] = mapped_column(Text())
    overview: Mapped[str] = mapped_column(Text())
    stories: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    model_name: Mapped[str] = mapped_column(Text())
    input_article_count: Mapped[int] = mapped_column(Integer)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
