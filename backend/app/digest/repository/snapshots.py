"""WeeklyTrendsSnapshot の永続化 Repository。

責務:
- 1 週分の bundle を 1 行 1 JSONB として ``weekly_trends_snapshots`` に保存する。
- 並行レース対策として ``insert_if_absent`` (``ON CONFLICT DO NOTHING``) を提供。
- ``upsert`` は ``--force`` 経由の手動再生成で使う (bundle と
  ``source_analysis_count`` を更新し、``generated_at`` も現在時刻で更新)。

snapshot は 1 単位保存が責務 (feedback_snapshot_responsibility.md)。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.weekly_trends_snapshot import WeeklyTrendsSnapshot


class SnapshotRepository:
    """``weekly_trends_snapshots`` への CRUD をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_latest(self) -> WeeklyTrendsSnapshot | None:
        """最新 (week_start DESC) の snapshot を 1 件返す (なければ None)。"""
        stmt = (
            select(WeeklyTrendsSnapshot)
            .order_by(WeeklyTrendsSnapshot.week_start.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_by_week(self, week_start: date) -> WeeklyTrendsSnapshot | None:
        """指定 ``week_start`` の snapshot を取得する (PK lookup)。"""
        return await self._session.get(WeeklyTrendsSnapshot, week_start)

    async def insert_if_absent(self, snapshot: WeeklyTrendsSnapshot) -> bool:
        """既存行が無ければ INSERT する。並行レースで負けたら ``False``。

        commit は呼び出し側 (Service) の責務。``ON CONFLICT (week_start)
        DO NOTHING`` で並行 INSERT の衝突を構造的に吸収する。
        """
        stmt = pg_insert(WeeklyTrendsSnapshot).values(
            week_start=snapshot.week_start,
            bundle=snapshot.bundle,
            source_analysis_count=snapshot.source_analysis_count,
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=["week_start"])
        result = await self._session.execute(stmt)
        return result.rowcount == 1

    async def upsert(self, snapshot: WeeklyTrendsSnapshot) -> None:
        """既存行があれば bundle / source_analysis_count を上書きする (``--force`` 用)。

        ``generated_at`` も同時に現在時刻で更新する (再生成の事実を記録)。
        commit は呼び出し側の責務。
        """
        stmt = pg_insert(WeeklyTrendsSnapshot).values(
            week_start=snapshot.week_start,
            bundle=snapshot.bundle,
            source_analysis_count=snapshot.source_analysis_count,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["week_start"],
            set_={
                "bundle": stmt.excluded.bundle,
                "source_analysis_count": stmt.excluded.source_analysis_count,
                "generated_at": func.now(),
            },
        )
        await self._session.execute(stmt)
