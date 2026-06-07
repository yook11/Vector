from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

__all__ = ["TrendsSnapshot"]


class TrendsSnapshot(Base):
    """rolling 7d window の集計結果を 1 行 1 JSONB として保持する snapshot。

    ``window_end`` は集計窓の上限 (半開区間 ``[window_end - 7d, window_end)``)
    で、JST 当日 0:00 の date。1 日 1 行で daily cron が INSERT する。
    ``bundle`` は ``TrendsBundle.model_dump(mode="json")`` 出力をそのまま
    格納する。snapshot は 1 単位保存が責務であり、推移分析や横断クエリのために
    正規化テーブル群に分解しない (feedback_snapshot_responsibility.md)。
    """

    __tablename__ = "trends_snapshots"
    __table_args__ = (
        CheckConstraint(
            "source_analysis_count >= 0",
            name="ck_trends_snapshots_count_non_negative",
        ),
        # find_latest (ORDER BY window_end DESC LIMIT 1) を高速化する DESC index。
        Index(
            "ix_trends_snapshots_window_end_desc",
            text("window_end DESC"),
        ),
    )

    window_end: Mapped[date] = mapped_column(primary_key=True)
    bundle: Mapped[dict[str, Any]] = mapped_column(JSONB)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    source_analysis_count: Mapped[int] = mapped_column()
