"""TrendsSnapshot の永続化 Repository。

責務:
- 1 集計窓分の bundle を 1 行 1 JSONB として ``trends_snapshots`` に保存する。
- ``exists_for_window_end``: cheap な exists 判定 (Pattern A' の `try_advance_from`
  precondition チェック用)。
- ``save``: ``ON CONFLICT (window_end) DO NOTHING RETURNING`` を基本とし、
  ``force=True`` のときは ``DO UPDATE`` 経路で既存行を上書きする。
  ``force=False`` の衝突は ``SnapshotSaveStatus.CONFLICT`` として返す。

snapshot は 1 単位保存が責務 (feedback_snapshot_responsibility.md)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trends_snapshot import TrendsSnapshot


class SnapshotSaveStatus(StrEnum):
    """``SnapshotRepository.save`` の永続化結果。"""

    INSERTED = "inserted"
    UPDATED = "updated"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class SnapshotSaveResult:
    """snapshot save の結果。"""

    status: SnapshotSaveStatus
    snapshot: TrendsSnapshot | None


class SnapshotRepository:
    """``trends_snapshots`` への CRUD をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_latest(self) -> TrendsSnapshot | None:
        """最新 (window_end DESC) の snapshot を 1 件返す (なければ None)。"""
        stmt = (
            select(TrendsSnapshot).order_by(TrendsSnapshot.window_end.desc()).limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_by_window_end(self, window_end: date) -> TrendsSnapshot | None:
        """指定 ``window_end`` の snapshot を取得する (PK lookup)。"""
        return await self._session.get(TrendsSnapshot, window_end)

    async def exists_for_window_end(self, window_end: date) -> bool:
        """`try_advance_from` 用 cheap exists 判定 (window_end 単位)。"""
        stmt = (
            select(TrendsSnapshot.window_end)
            .where(TrendsSnapshot.window_end == window_end)
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def save(
        self,
        snapshot: TrendsSnapshot,
        *,
        force: bool = False,
    ) -> SnapshotSaveResult:
        """snapshot を ``trends_snapshots`` に永続化する。

        commit は呼び出し側 (Service) の責務。

        Args:
            snapshot: 永続化する TrendsSnapshot (window_end / bundle /
                source_analysis_count)
            force: ``True`` のとき既存行を上書きし ``generated_at`` を現在時刻に
                更新する (手動再生成経路)。``False`` (default) のときは新規 INSERT
                のみで、衝突時は副作用なしに ``CONFLICT`` を返す。

        Returns:
            永続化成功時: 永続化後の ``TrendsSnapshot``
            ``SnapshotSaveResult``。``force=False`` の衝突時だけ
            ``snapshot=None`` になる。
        """
        existed = False
        if force:
            existed = await self.exists_for_window_end(snapshot.window_end)
            stmt = (
                pg_insert(TrendsSnapshot)
                .values(
                    window_end=snapshot.window_end,
                    bundle=snapshot.bundle,
                    source_analysis_count=snapshot.source_analysis_count,
                )
                .on_conflict_do_update(
                    index_elements=["window_end"],
                    set_={
                        "bundle": snapshot.bundle,
                        "source_analysis_count": snapshot.source_analysis_count,
                        "generated_at": func.now(),
                    },
                )
                .returning(
                    TrendsSnapshot.window_end,
                    TrendsSnapshot.generated_at,
                )
            )
        else:
            stmt = (
                pg_insert(TrendsSnapshot)
                .values(
                    window_end=snapshot.window_end,
                    bundle=snapshot.bundle,
                    source_analysis_count=snapshot.source_analysis_count,
                )
                .on_conflict_do_nothing(index_elements=["window_end"])
                .returning(
                    TrendsSnapshot.window_end,
                    TrendsSnapshot.generated_at,
                )
            )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return SnapshotSaveResult(
                status=SnapshotSaveStatus.CONFLICT,
                snapshot=None,
            )
        saved = TrendsSnapshot(
            window_end=row.window_end,
            bundle=snapshot.bundle,
            source_analysis_count=snapshot.source_analysis_count,
            generated_at=row.generated_at,
        )
        status = (
            SnapshotSaveStatus.UPDATED
            if force and existed
            else SnapshotSaveStatus.INSERTED
        )
        return SnapshotSaveResult(status=status, snapshot=saved)
