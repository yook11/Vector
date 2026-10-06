"""トレンドの開始条件を確認し、集計・保存・監査・通知を順に行う。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.domain.event import EventType
from app.audit.stages.trend_discovery import (
    TrendDiscoveryOutcomeCode,
    append_trend_discovery_run_event_best_effort,
)
from app.insights.trend_discovery.domain.ready import (
    ReadyForTrendDiscovery,
    TrendDiscoveryReadyBuildRejectionReason,
)
from app.insights.trend_discovery.domain.trend import (
    CategoryTrends,
    TrendsBundle,
    TrendWeeks,
    rank_mention_trends,
)
from app.insights.trend_discovery.repository import (
    SnapshotRepository,
    SnapshotSaveStatus,
    TrendsRepository,
)
from app.insights.trend_discovery.schemas import trends_from_snapshot
from app.models.category import Category
from app.models.trends_snapshot import TrendsSnapshot
from app.shared.revalidate import RevalidateNotifier

logger = structlog.get_logger(__name__)

# フロントエンドのcacheTags.trendsと揃える。
TRENDS_REVALIDATE_TAGS: tuple[str, ...] = ("trends",)


@dataclass(frozen=True, slots=True)
class TrendDiscoveryCompleted:
    """トレンドの集計結果を保存した。"""

    snapshot_date: date
    analyzed_article_count: int
    completed_category_count: int


@dataclass(frozen=True, slots=True)
class SkippedAlreadyGenerated:
    """同じ日付のスナップショットが保存済みのため開始しなかった。"""

    snapshot_date: date


@dataclass(frozen=True, slots=True)
class SkippedNoTargetArticles:
    """対象の分析済み記事がないため生成しなかった。"""

    snapshot_date: date
    analyzed_article_count: int = 0
    completed_category_count: int | None = None


@dataclass(frozen=True, slots=True)
class TrendDiscoveryConflict:
    """同時実行した別のワーカーが先に保存したため、保存しなかった。"""

    snapshot_date: date
    analyzed_article_count: int
    completed_category_count: int


TrendDiscoveryOutcome = (
    TrendDiscoveryCompleted
    | SkippedAlreadyGenerated
    | SkippedNoTargetArticles
    | TrendDiscoveryConflict
)


class TrendDiscoveryService:
    """トレンドの準備・集計・保存と、結果に応じた監査・通知を担う。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, notifier: RevalidateNotifier) -> None:
        """直近の完了済み7日間のトレンドを作成する。"""
        weeks = TrendWeeks.latest(datetime.now(UTC))
        try:
            outcome = await self.execute(weeks)
        except Exception as exc:
            await append_trend_discovery_run_event_best_effort(
                self._session_factory,
                event_type=EventType.FAILED,
                outcome_code=TrendDiscoveryOutcomeCode.RUN_FAILED,
                window_start=weeks.week.start,
                window_end=weeks.snapshot_date,
                exc=exc,
            )
            raise

        if isinstance(outcome, SkippedAlreadyGenerated):
            logger.info(
                "trend_discovery_task_skipped_already_exists",
                window_end=outcome.snapshot_date.isoformat(),
            )
            return
        if isinstance(outcome, SkippedNoTargetArticles):
            logger.info(
                "trend_discovery_task_skipped_no_target_articles",
                window_end=outcome.snapshot_date.isoformat(),
            )
            return
        if isinstance(outcome, TrendDiscoveryConflict):
            logger.info(
                "trend_discovery_task_conflict",
                window_end=outcome.snapshot_date.isoformat(),
                source_analysis_count=outcome.analyzed_article_count,
                category_count=outcome.completed_category_count,
            )
            return

        await append_trend_discovery_run_event_best_effort(
            self._session_factory,
            event_type=EventType.SUCCEEDED,
            outcome_code=TrendDiscoveryOutcomeCode.RUN_COMPLETED,
            window_start=weeks.week.start,
            window_end=outcome.snapshot_date,
            source_analysis_count=outcome.analyzed_article_count,
            completed_category_count=outcome.completed_category_count,
        )

        logger.info(
            "trend_discovery_task_completed",
            window_end=outcome.snapshot_date.isoformat(),
            source_analysis_count=outcome.analyzed_article_count,
            category_count=outcome.completed_category_count,
        )
        await notifier.notify(tags=TRENDS_REVALIDATE_TAGS)

    async def execute(self, weeks: TrendWeeks) -> TrendDiscoveryOutcome:
        """準備から保存までを同じセッションで扱い、開始条件を満たした週を集計する。"""
        async with self._session_factory() as session:
            facts = await TrendsRepository(session).load_ready_build_facts(weeks=weeks)
            ready = ReadyForTrendDiscovery.from_facts(weeks=weeks, facts=facts)
            if ready is TrendDiscoveryReadyBuildRejectionReason.ALREADY_GENERATED:
                return SkippedAlreadyGenerated(snapshot_date=weeks.snapshot_date)
            if ready is TrendDiscoveryReadyBuildRejectionReason.NO_ARTICLES:
                return SkippedNoTargetArticles(snapshot_date=weeks.snapshot_date)
            outcome = await self._generate(session, ready)
            await session.commit()
            return outcome

    async def _generate(
        self, session: AsyncSession, ready: ReadyForTrendDiscovery
    ) -> TrendDiscoveryCompleted | TrendDiscoveryConflict:
        """開始条件を満たした週を集計し、既存行を上書きせず保存する。"""
        weeks = ready.weeks
        trends_repo = TrendsRepository(session)
        categories = await trends_repo.get_categories()
        category_trends_list: list[CategoryTrends] = []
        for cat in categories:
            category_trends_list.append(
                await self._build_category_trends(
                    trends_repo, category=cat, weeks=weeks
                )
            )
        category_trends = tuple(category_trends_list)
        completed_category_count = len(category_trends)
        bundle = TrendsBundle(weeks=weeks, category_trends=category_trends)
        generated_at = datetime.now(UTC)
        response = trends_from_snapshot(
            bundle=bundle,
            generated_at=generated_at,
            analyzed_article_count=ready.analyzed_article_count,
        )
        snapshot = TrendsSnapshot(
            window_end=weeks.snapshot_date,
            bundle=response.model_dump(mode="json", by_alias=True),
            source_analysis_count=ready.analyzed_article_count,
            generated_at=generated_at,
        )
        save_result = await SnapshotRepository(session).save(snapshot)
        if save_result.status == SnapshotSaveStatus.CONFLICT:
            return TrendDiscoveryConflict(
                snapshot_date=weeks.snapshot_date,
                analyzed_article_count=ready.analyzed_article_count,
                completed_category_count=completed_category_count,
            )
        return TrendDiscoveryCompleted(
            snapshot_date=weeks.snapshot_date,
            analyzed_article_count=ready.analyzed_article_count,
            completed_category_count=completed_category_count,
        )

    @staticmethod
    async def _build_category_trends(
        trends_repo: TrendsRepository,
        *,
        category: Category,
        weeks: TrendWeeks,
    ) -> CategoryTrends:
        """候補に順位を付け、公開する名前にだけ要点と一緒に語られた名前を付ける。"""
        candidates = await trends_repo.get_mention_candidates(
            category_id=category.id, weeks=weeks
        )
        mention_trends = rank_mention_trends(candidates)
        mention_keys = [(t.name.match_key, t.type.value) for t in mention_trends]
        key_points = await trends_repo.get_mention_key_points(
            category_id=category.id, week=weeks.week, mention_keys=mention_keys
        )
        co_mentions = await trends_repo.get_co_mentions(
            category_id=category.id, week=weeks.week, mention_keys=mention_keys
        )
        return CategoryTrends(
            category_slug=category.slug,
            category_name=category.name,
            mention_trends=tuple(
                trend.model_copy(
                    update={
                        "key_points": key_points.get(key, ()),
                        "mentioned_with": co_mentions.get(key, ()),
                    }
                )
                for trend, key in zip(mention_trends, mention_keys, strict=True)
            ),
        )
