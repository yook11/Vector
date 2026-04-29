"""WeeklyTrendsSnapshot の永続化 Repository。

責務:
- 1 週分の bundle を 1 行 1 JSONB として ``weekly_trends_snapshots`` に保存する。
- ``exists_for_week``: cheap な exists 判定 (Pattern A' の `try_advance_from`
  precondition チェック用)。
- ``save``: ``ON CONFLICT (week_start) DO NOTHING RETURNING`` を基本とし、
  ``force=True`` のときは ``DO UPDATE`` 経路で既存行を上書きする。race 敗北
  (force=False かつ既存あり) は ``None`` 戻りで Service が ``find_by_week`` で
  読戻す (spec §4.6、Phase 1-3 同型)。

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
        """指定 ``week_start`` の snapshot を取得する (PK lookup、race 読戻し用)。"""
        return await self._session.get(WeeklyTrendsSnapshot, week_start)

    async def exists_for_week(self, week_start: date) -> bool:
        """`try_advance_from` 用 cheap exists 判定 (week_start 単位)。"""
        stmt = (
            select(WeeklyTrendsSnapshot.week_start)
            .where(WeeklyTrendsSnapshot.week_start == week_start)
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def save(
        self,
        snapshot: WeeklyTrendsSnapshot,
        *,
        force: bool = False,
    ) -> WeeklyTrendsSnapshot | None:
        """snapshot を ``weekly_trends_snapshots`` に永続化する。

        commit は呼び出し側 (Service) の責務。

        Args:
            snapshot: 永続化する WeeklyTrendsSnapshot (week_start / bundle /
                source_analysis_count)
            force: ``True`` のとき既存行を上書きし ``generated_at`` を現在時刻に
                更新する (手動再生成経路)。``False`` (default) のときは新規 INSERT
                のみで、衝突時は副作用なしに ``None`` を返す。

        Returns:
            永続化成功時: 永続化後の ``WeeklyTrendsSnapshot``
            race 敗北時 (force=False かつ既存あり): ``None`` (Service が
            ``find_by_week`` で勝者を読み戻す — spec §4.6)。``force=True`` 経路
            では常に Snapshot を返す。
        """
        if force:
            stmt = (
                pg_insert(WeeklyTrendsSnapshot)
                .values(
                    week_start=snapshot.week_start,
                    bundle=snapshot.bundle,
                    source_analysis_count=snapshot.source_analysis_count,
                )
                .on_conflict_do_update(
                    index_elements=["week_start"],
                    set_={
                        "bundle": snapshot.bundle,
                        "source_analysis_count": snapshot.source_analysis_count,
                        "generated_at": func.now(),
                    },
                )
                .returning(
                    WeeklyTrendsSnapshot.week_start,
                    WeeklyTrendsSnapshot.generated_at,
                )
            )
        else:
            stmt = (
                pg_insert(WeeklyTrendsSnapshot)
                .values(
                    week_start=snapshot.week_start,
                    bundle=snapshot.bundle,
                    source_analysis_count=snapshot.source_analysis_count,
                )
                .on_conflict_do_nothing(index_elements=["week_start"])
                .returning(
                    WeeklyTrendsSnapshot.week_start,
                    WeeklyTrendsSnapshot.generated_at,
                )
            )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return WeeklyTrendsSnapshot(
            week_start=row.week_start,
            bundle=snapshot.bundle,
            source_analysis_count=snapshot.source_analysis_count,
            generated_at=row.generated_at,
        )
