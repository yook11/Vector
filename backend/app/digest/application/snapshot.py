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

戻り値の tagged union:
- ``Generated``: 新規生成または ``--force`` 上書きで snapshot を保存した
- ``Skipped``: 既存 snapshot があり ``force=False`` だったため何もしなかった
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.digest.config import DEFAULT_LIMIT, NEW_ENTITY_LOOKBACK_WEEKS, WEEK_TZ
from app.digest.domain.trend import WeeklyCategoryTrends, WeeklyTrendsBundle
from app.digest.repository.snapshots import SnapshotRepository
from app.digest.repository.trends import TrendsRepository
from app.models.category import Category
from app.models.weekly_trends_snapshot import WeeklyTrendsSnapshot

logger = structlog.get_logger(__name__)

_WEEK = timedelta(days=7)


# ---------------------------------------------------------------------------
# Outcome — Service 戻り値の tagged union
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Generated:
    """新規生成または ``--force`` 上書きで snapshot を保存した。"""

    week_start: date
    source_analysis_count: int


@dataclass(frozen=True, slots=True)
class Skipped:
    """既存 snapshot があり ``force=False`` だったため何もしなかった。"""

    week_start: date


SnapshotResult = Generated | Skipped


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class WeeklyTrendsSnapshotService:
    """1 週分の weekly trends snapshot を生成・永続化するユースケース。

    1 session = 1 トランザクションとして集計と INSERT を atomic に実行する。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def generate_for_latest_completed_week(
        self, *, force: bool = False
    ) -> SnapshotResult:
        """直近完了週 (= 今がいる週の前週) を JST 月曜起点で算出して生成する。"""
        now = datetime.now(ZoneInfo(WEEK_TZ))
        week_start = self._completed_week_start_for(now)
        return await self.generate_for_week(week_start, force=force)

    async def generate_for_week(
        self, week_start: date, *, force: bool = False
    ) -> SnapshotResult:
        """指定 week (JST 月曜開始日) の snapshot を生成する。"""
        async with self._session_factory() as session:
            snapshot_repo = SnapshotRepository(session)

            if not force:
                existing = await snapshot_repo.find_by_week(week_start)
                if existing is not None:
                    logger.info(
                        "snapshot_skipped_existing",
                        week_start=week_start.isoformat(),
                    )
                    return Skipped(week_start=week_start)

            current_start = self._jst_midnight_utc(week_start)
            current_end = current_start + _WEEK
            previous_start = current_start - _WEEK
            lookback_start = current_start - _WEEK * NEW_ENTITY_LOOKBACK_WEEKS

            trends_repo = TrendsRepository(session)
            categories = await self._fetch_categories(session)

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
            bundle = WeeklyTrendsBundle(week_start=week_start, sections=sections)

            snapshot = WeeklyTrendsSnapshot(
                week_start=week_start,
                bundle=bundle.model_dump(mode="json"),
                source_analysis_count=source_count,
            )
            if force:
                await snapshot_repo.upsert(snapshot)
            else:
                inserted = await snapshot_repo.insert_if_absent(snapshot)
                if not inserted:
                    # 並行レース敗北: 別プロセスが先に generate した
                    await session.rollback()
                    logger.info(
                        "snapshot_skipped_concurrent_insert",
                        week_start=week_start.isoformat(),
                    )
                    return Skipped(week_start=week_start)

            await session.commit()
            logger.info(
                "snapshot_generated",
                week_start=week_start.isoformat(),
                category_count=len(sections),
                source_analysis_count=source_count,
                forced=force,
            )
            return Generated(week_start=week_start, source_analysis_count=source_count)

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

    @staticmethod
    def _completed_week_start_for(now: datetime) -> date:
        """``now`` (JST 想定の tz-aware datetime) における直近完了週の月曜日。

        例: JST 2026-04-27 (月) 00:05 → 2026-04-20 (= 前週月曜)
            JST 2026-04-26 (日) 23:50 → 2026-04-13 (= 完了済み週の月曜)
        """
        today = now.date()
        days_since_monday = today.weekday()
        current_monday = today - timedelta(days=days_since_monday)
        return current_monday - _WEEK
