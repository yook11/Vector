"""WeeklyTrendsSnapshotService — 1 週分の集計 → bundle 構築 → INSERT を 1 ユース
ケースとして組み立てる。

責務:
- 1 ユースケース = 1 session = 1 トランザクション (集計 SELECT も snapshot
  INSERT も同一トランザクション内で実行する)
- 集計対象の window は ``[current_start, current_end)`` を JST 月曜 00:00 起点
  で計算し、UTC-aware datetime に変換して repository に渡す
- snapshot は 1 単位保存が責務 (feedback_snapshot_responsibility.md)
- 例外は捕まえず raise する (CLI / Task の retry に委ねる:
  feedback_failure_visibility.md)

Pattern A' での Stage F:
- 起動時に ``ReadyForDigest`` を受け取り、precondition (既存 snapshot 判定) は
  Ready 側で吸収済み
- ``execute(ready)`` は集計 + save に専念し、戻り値は ``Generated`` の単一
  variant
- race 敗北 (force=False で同時 INSERT 競合) は ``find_by_week`` で勝者を読み戻し
  ``Generated`` に合流する (Phase 1-3 同型)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.insights.snapshot.config import (
    DEFAULT_LIMIT,
    NEW_ENTITY_LOOKBACK_WEEKS,
    WEEK_TZ,
)
from app.insights.snapshot.domain.ready import ReadyForDigest
from app.insights.snapshot.domain.trend import WeeklyCategoryTrends, WeeklyTrendsBundle
from app.insights.snapshot.repository.snapshots import SnapshotRepository
from app.insights.snapshot.repository.trends import TrendsRepository
from app.models.category import Category
from app.models.weekly_trends_snapshot import WeeklyTrendsSnapshot

logger = structlog.get_logger(__name__)

_WEEK = timedelta(days=7)


# ---------------------------------------------------------------------------
# Outcome — Service 戻り値の単一 variant
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Generated:
    """新規生成または ``force=True`` 上書きで snapshot を保存した。

    既存 snapshot ありかつ ``force=False`` の skip ケースは ``Ready.try_advance_from``
    で吸収済みのため Service.execute の戻り値からは消えている (Pattern A')。
    """

    week_start: date
    source_analysis_count: int


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class WeeklyTrendsSnapshotService:
    """1 週分の weekly trends snapshot を生成・永続化するユースケース。

    1 session = 1 トランザクションとして集計と INSERT を atomic に実行する。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(self, ready: ReadyForDigest) -> Generated:
        """``ready`` で指定された週の snapshot を集計・永続化する。

        precondition (既存 snapshot 判定) は ``ReadyForDigest.try_advance_from``
        側で吸収済み。本メソッドは集計 + save に専念する。

        race 敗北 (``force=False`` 経路で同時 INSERT 競合) は ``find_by_week`` で
        勝者を読み戻し ``Generated`` に合流する (Phase 1-3 同型)。
        """
        async with self._session_factory() as session:
            snapshot_repo = SnapshotRepository(session)
            trends_repo = TrendsRepository(session)
            categories = await self._fetch_categories(session)

            current_start = self._jst_midnight_utc(ready.week_start)
            current_end = current_start + _WEEK
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
            source_count = await trends_repo.count_source_analyses(
                current_start=current_start, current_end=current_end
            )
            bundle = WeeklyTrendsBundle(week_start=ready.week_start, sections=sections)

            snapshot = WeeklyTrendsSnapshot(
                week_start=ready.week_start,
                bundle=bundle.model_dump(mode="json"),
                source_analysis_count=source_count,
            )
            saved = await snapshot_repo.save(snapshot, force=ready.force)
            await session.commit()

            if saved is None:
                # race 敗北 (force=False で他 worker が先行 INSERT): 勝者を読み戻す
                logger.info(
                    "digest_concurrent_write",
                    week_start=ready.week_start.isoformat(),
                )
                saved = await snapshot_repo.find_by_week(ready.week_start)
                if saved is None:
                    raise RuntimeError(
                        "digest_race_winner_missing: "
                        f"week_start={ready.week_start.isoformat()}"
                    )

            logger.info(
                "snapshot_generated",
                week_start=ready.week_start.isoformat(),
                category_count=len(sections),
                source_analysis_count=source_count,
                forced=ready.force,
            )
            return Generated(
                week_start=ready.week_start,
                source_analysis_count=source_count,
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
        topics = await trends_repo.get_trending_topics(
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
        # 膨らむため、各リストを上位 ``DEFAULT_LIMIT`` 件で truncate して JSONB 肥大化と
        # UI ノイズを構造的に抑える (hot 系は閾値で既に小さいが対称性のため同じ扱い)。
        return WeeklyCategoryTrends(
            category_id=category.id,
            category_slug=category.slug,
            category_name=category.name,
            trending_entities=entities[:DEFAULT_LIMIT],
            trending_topics=topics[:DEFAULT_LIMIT],
            new_entities=new_entities[:DEFAULT_LIMIT],
        )

    @staticmethod
    async def _fetch_categories(session: AsyncSession) -> tuple[Category, ...]:
        stmt = select(Category).order_by(Category.id)
        rows = (await session.execute(stmt)).scalars().all()
        return tuple(rows)

    @staticmethod
    def _jst_midnight_utc(week_start: date) -> datetime:
        """JST 月曜 00:00 を UTC-aware datetime に変換する。"""
        jst_midnight = datetime(
            week_start.year,
            week_start.month,
            week_start.day,
            tzinfo=ZoneInfo(WEEK_TZ),
        )
        return jst_midnight.astimezone(UTC)
