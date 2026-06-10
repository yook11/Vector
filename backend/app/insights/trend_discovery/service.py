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

from app.insights.trend_discovery.domain.ready import ReadyForTrendDiscovery
from app.insights.trend_discovery.domain.trend import (
    CategoryTrends,
    MentionKey,
    RankedMention,
    TrendsBundle,
    select_fastest_growing,
    select_most_mentioned,
)
from app.insights.trend_discovery.domain.window import WEEK_TZ
from app.insights.trend_discovery.repository import (
    SnapshotRepository,
    SnapshotSaveStatus,
    TrendsRepository,
)
from app.insights.trend_discovery.schemas import trends_from_snapshot
from app.models.category import Category
from app.models.trends_snapshot import TrendsSnapshot

logger = structlog.get_logger(__name__)

_WEEK = timedelta(days=7)

# 生成成功後に frontend へ revalidate を打つ cache tag。frontend の
# lib/cache/tags.ts (cacheTags.trends) と一致させること。task / CLI 双方が
# 同値を使うよう単一定義する (誤変更を防ぐため不変 tuple)。
TRENDS_REVALIDATE_TAGS: tuple[str, ...] = ("trends",)


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
        - ``previous = [window_end - 14d, window_end - 7d)`` (伸び率の前週比較用)

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

            category_trends_list: list[CategoryTrends] = []
            for cat in categories:
                category_trends_list.append(
                    await self._build_category_trends(
                        trends_repo,
                        category=cat,
                        current_start=current_start,
                        current_end=current_end,
                        previous_start=previous_start,
                    )
                )
            category_trends = tuple(category_trends_list)
            completed_category_count = len(category_trends)
            bundle = TrendsBundle(
                window_end=ready.window_end, category_trends=category_trends
            )

            # snapshot は API レスポンス (camelCase) をそのまま焼く。読取側は保存済
            # bundle を Trends schema で再検証してから返す (router)。generated_at は
            # JSON と DB列の双方へ同値を入れるためアプリ側で1つ確定する。
            generated_at = datetime.now(UTC)
            response = trends_from_snapshot(
                bundle=bundle,
                generated_at=generated_at,
                source_analysis_count=source_count,
            )
            snapshot = TrendsSnapshot(
                window_end=ready.window_end,
                bundle=response.model_dump(mode="json", by_alias=True),
                source_analysis_count=source_count,
                generated_at=generated_at,
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
    async def _build_category_trends(
        trends_repo: TrendsRepository,
        *,
        category: Category,
        current_start: datetime,
        current_end: datetime,
        previous_start: datetime,
    ) -> CategoryTrends:
        """1 カテゴリ分の 2 ランキングを確定し、上位 mention に文脈を添えて束ねる。

        repository は floor 通過の全 mention を母集団として返し、2 ランキングの確定
        (母集団の違い・hot ゲート含む) は domain の選定関数に委ねる。両ランキング
        の和集合だけ key_point / related mention を取得し (1 カテゴリ 3 query)、
        同一 mention が両方に載る場合は同じ enrich 済みインスタンスを共有する。
        """
        pool = await trends_repo.get_ranked_mentions(
            category_id=category.id,
            current_start=current_start,
            current_end=current_end,
            previous_start=previous_start,
        )
        most_mentioned = select_most_mentioned(pool)
        fastest_growing = select_fastest_growing(pool)

        union: dict[MentionKey, RankedMention] = {}
        for mention in (*most_mentioned, *fastest_growing):
            union.setdefault((mention.name.match_key, mention.type.value), mention)
        mention_keys = list(union.keys())

        key_points = await trends_repo.get_mention_key_points(
            category_id=category.id,
            current_start=current_start,
            current_end=current_end,
            mention_keys=mention_keys,
        )
        related = await trends_repo.get_related_mentions(
            category_id=category.id,
            current_start=current_start,
            current_end=current_end,
            mention_keys=mention_keys,
        )
        enriched = {
            key: mention.model_copy(
                update={
                    "key_points": key_points.get(key, ()),
                    "related_mentions": related.get(key, ()),
                }
            )
            for key, mention in union.items()
        }

        def _with_context(mention: RankedMention) -> RankedMention:
            return enriched[(mention.name.match_key, mention.type.value)]

        return CategoryTrends(
            category_id=category.id,
            category_slug=category.slug,
            category_name=category.name,
            most_mentioned=tuple(_with_context(m) for m in most_mentioned),
            fastest_growing=tuple(_with_context(m) for m in fastest_growing),
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
