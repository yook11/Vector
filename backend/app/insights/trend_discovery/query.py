"""TrendsQueryService — トレンド snapshot の Read 経路。

責務:
- 既存 snapshot の取り出し (``find_latest`` のみ。Phase 1A は最新週単一表示)
- ``AsyncSession`` を直接 DI (FastAPI ``Depends(get_session)`` 経由)。
  Read は session_factory による独立トランザクションを必要としないため、
  ``TrendDiscoveryService`` とは異なる DI 形式を取る (CQRS 風分離)

例外方針 (feedback_failure_visibility.md):
- bundle JSONB の validate 失敗等は Router 側で 500 として表面化させる。
  この層では捕まえない
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.trend_discovery.repository import SnapshotRepository
from app.models.trends_snapshot import TrendsSnapshot


class TrendsQueryService:
    """トレンド snapshot の Read ユースケースをまとめる薄い Service。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_latest(self) -> TrendsSnapshot | None:
        """最新 (window_end DESC) の snapshot を 1 件返す (なければ None)。"""
        repo = SnapshotRepository(self._session)
        return await repo.find_latest()
