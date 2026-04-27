from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["WeeklyTrendsSnapshot"]


class WeeklyTrendsSnapshot(Base):
    """週次トレンドの 1 週間分まとまりを 1 行 1 JSONB として保持する snapshot。

    ``bundle`` は ``WeeklyTrendsBundle.model_dump(mode="json")`` 出力をそのまま
    格納する。snapshot は 1 単位保存が責務であり、推移分析や横断クエリのために
    正規化テーブル群に分解しない (feedback_snapshot_responsibility.md)。
    """

    __tablename__ = "weekly_trends_snapshots"
    __table_args__ = (
        CheckConstraint(
            "source_analysis_count >= 0",
            name="ck_weekly_trends_snapshots_count_non_negative",
        ),
        # find_latest (ORDER BY week_start DESC LIMIT 1) を高速化する DESC index。
        Index(
            "ix_weekly_trends_snapshots_week_start_desc",
            text("week_start DESC"),
        ),
    )

    week_start: Mapped[date] = mapped_column(primary_key=True)
    bundle: Mapped[dict[str, Any]] = mapped_column(JSONB)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    source_analysis_count: Mapped[int] = mapped_column()
