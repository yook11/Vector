"""TrendDiscoveryService — rolling 7d の trend 発見 → bundle 構築 → INSERT を 1 ユース
ケースとして組み立てる。

責務:
- 1 ユースケース = 1 session = 1 トランザクション (集計 SELECT も snapshot
  INSERT も同一トランザクション内で実行する)
- 集計対象の window は ``[current_start, current_end)`` を JST 00:00 起点
  で計算し、UTC-aware datetime に変換して repository に渡す
- snapshot は 1 単位保存が責務 (feedback_snapshot_responsibility.md)
- 例外は捕まえず raise する (CLI / Task の retry に委ねる:
  feedback_failure_visibility.md)

Pattern A' での Stage F:
- 起動時に ``ReadyForTrendDiscovery`` を受け取り、precondition (既存 snapshot 判定) は
  Ready 側で吸収済み
- ``execute(ready)`` は集計対象記事の件数を先に確認し、0 件なら保存せず
  ``SkippedNoTargetArticles`` を返す
- race 敗北 (force=False で同時 INSERT 競合) は読み戻しせず
  ``TrendDiscoveryConflict`` を返す
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.insights.trend_discovery.config import (
    NEW_ENTITY_LOOKBACK_WEEKS,
    WEEK_TZ,
)
from app.insights.trend_discovery.domain.ready import ReadyForTrendDiscovery
from app.insights.trend_discovery.domain.trend import (
    MAX_TRENDS_PER_CATEGORY,
    WeeklyCategoryTrends,
    WeeklyTrendsBundle,
)
from app.insights.trend_discovery.repository.snapshots import (
    SnapshotRepository,
    SnapshotSaveStatus,
)
from app.insights.trend_discovery.repository.trends import TrendsRepository
from app.models.category import Category
from app.models.weekly_trends_snapshot import WeeklyTrendsSnapshot

logger = structlog.get_logger(__name__)

_WEEK = timedelta(days=7)


# ---------------------------------------------------------------------------
# Outcome — Service 戻り値
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrendDiscoveryCompleted:
    """trend discovery が完了し、snapshot を保存した。

    既存 snapshot ありかつ ``force=False`` の skip ケースは ``Ready.try_advance_from``
    で吸収済みのため Service.execute の戻り値からは消えている (Pattern A')。
    """

    window_end: date
    source_analysis_count: int
    completed_category_count: int
    updated: bool = False


@dataclass(frozen=True, slots=True)
class SkippedNoTargetArticles:
    """snapshot 集計対象の分析済み記事が 0 件のため生成を行わなかった。"""

    window_end: date
    source_analysis_count: int = 0
    completed_category_count: int | None = None


@dataclass(frozen=True, slots=True)
class TrendDiscoveryConflict:
    """同時実行により別 worker が先に保存したため、自 worker は保存しなかった。"""

    window_end: date
    source_analysis_count: int
    completed_category_count: int


TrendDiscoveryOutcome = (
    TrendDiscoveryCompleted | SkippedNoTargetArticles | TrendDiscoveryConflict
)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TrendDiscoveryService:
    """rolling 7d の分析済み記事から trend snapshot を生成・永続化するユースケース。

    1 session = 1 トランザクションとして集計と INSERT を atomic に実行する。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(self, ready: ReadyForTrendDiscovery) -> TrendDiscoveryOutcome:
        """``ready`` で指定された window_end の snapshot を集計・永続化する。

        precondition (既存 snapshot 判定) は ``ReadyForTrendDiscovery.try_advance_from``
        側で吸収済み。本メソッドは集計対象記事の件数を確認し、0 件なら
        category 集計・保存に進まない。

        集計窓は rolling 7d:
        - ``current  = [window_end - 7d, window_end)``
        - ``previous = [window_end - 14d, window_end - 7d)``
        - ``lookback = [window_end - 7d - 4w, window_end - 7d)``
          (現状窓の手前 4 週、new entity 初出判定用)

        race 敗北 (``force=False`` 経路で同時 INSERT 競合) は読み戻しせず
        ``TrendDiscoveryConflict`` を返す。
        """
        async with self._session_factory() as session:
            snapshot_repo = SnapshotRepository(session)
            trends_repo = TrendsRepository(session)

            current_end = self._jst_midnight_utc(ready.window_end)
            current_start = current_end - _WEEK
            source_count = await trends_repo.count_source_analyses(
                current_start=current_start, current_end=current_end
            )
            if source_count == 0:
                logger.info(
                    "trend_discovery_skipped_no_target_articles",
                    window_end=ready.window_end.isoformat(),
                    forced=ready.force,
                )
                return SkippedNoTargetArticles(window_end=ready.window_end)

            categories = await self._fetch_categories(session)
            previous_start = current_start - _WEEK
            lookback_start = current_start - _WEEK * NEW_ENTITY_LOOKBACK_WEEKS

            sections_list: list[WeeklyCategoryTrends] = []
            for cat in categories:
                sections_list.append(
                    await self._build_section(
                        trends_repo,
                        category=cat,
                        current_start=current_start,
                        current_end=current_end,
                        previous_start=previous_start,
                        lookback_start=lookback_start,
                    )
                )
            sections = tuple(sections_list)
            completed_category_count = len(sections)
            bundle = WeeklyTrendsBundle(window_end=ready.window_end, sections=sections)

            snapshot = WeeklyTrendsSnapshot(
                window_end=ready.window_end,
                bundle=bundle.model_dump(mode="json"),
                source_analysis_count=source_count,
            )
            save_result = await snapshot_repo.save(snapshot, force=ready.force)
            await session.commit()

            if save_result.status == SnapshotSaveStatus.CONFLICT:
                logger.info(
                    "trend_discovery_conflict",
                    window_end=ready.window_end.isoformat(),
                    category_count=completed_category_count,
                    source_analysis_count=source_count,
                )
                return TrendDiscoveryConflict(
                    window_end=ready.window_end,
                    source_analysis_count=source_count,
                    completed_category_count=completed_category_count,
                )

            logger.info(
                "trend_discovery_completed",
                window_end=ready.window_end.isoformat(),
                category_count=completed_category_count,
                source_analysis_count=source_count,
                forced=ready.force,
                save_status=save_result.status.value,
            )
            return TrendDiscoveryCompleted(
                window_end=ready.window_end,
                source_analysis_count=source_count,
                completed_category_count=completed_category_count,
                updated=save_result.status == SnapshotSaveStatus.UPDATED,
            )

    @staticmethod
    async def _build_section(
        trends_repo: TrendsRepository,
        *,
        category: Category,
        current_start: datetime,
        current_end: datetime,
        previous_start: datetime,
        lookback_start: datetime,
    ) -> WeeklyCategoryTrends:
        entities = await trends_repo.get_trending_entities(
            category_id=category.id,
            current_start=current_start,
            current_end=current_end,
            previous_start=previous_start,
        )
        new_entities = await trends_repo.get_new_entities(
            category_id=category.id,
            current_start=current_start,
            current_end=current_end,
            lookback_start=lookback_start,
        )
        # new entity の集計は閾値が緩く (current_count >= 1) 1 カテゴリで 1000+ 件に
        # 膨らむため、各リストを上位 ``MAX_TRENDS_PER_CATEGORY`` 件で truncate して
        # JSONB 肥大化と UI ノイズを構造的に抑える (hot 系は閾値で既に小さいが対称性
        # のため同じ扱い)。同定数は WeeklyCategoryTrends の Field(max_length=...)
        # でも参照され、生成側 truncate と VO 不変条件の SSoT になっている。
        return WeeklyCategoryTrends(
            category_id=category.id,
            category_slug=category.slug,
            category_name=category.name,
            trending_entities=entities[:MAX_TRENDS_PER_CATEGORY],
            new_entities=new_entities[:MAX_TRENDS_PER_CATEGORY],
        )

    @staticmethod
    async def _fetch_categories(session: AsyncSession) -> tuple[Category, ...]:
        stmt = select(Category).order_by(Category.id)
        rows = (await session.execute(stmt)).scalars().all()
        return tuple(rows)

    @staticmethod
    def _jst_midnight_utc(target_date: date) -> datetime:
        """JST 当日 00:00 を UTC-aware datetime に変換する。"""
        jst_midnight = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            tzinfo=ZoneInfo(WEEK_TZ),
        )
        return jst_midnight.astimezone(UTC)
