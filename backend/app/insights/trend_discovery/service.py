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
    MentionKey,
    RankedMention,
    TrendsBundle,
    TrendWindow,
    select_fastest_growing,
    select_most_mentioned,
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

    window_end: date
    source_analysis_count: int
    completed_category_count: int


@dataclass(frozen=True, slots=True)
class SkippedAlreadyGenerated:
    """同じ期間の生成結果が保存済みのため開始しなかった。"""

    window_end: date


@dataclass(frozen=True, slots=True)
class SkippedNoTargetArticles:
    """対象の分析済み記事がないため生成しなかった。"""

    window_end: date
    source_analysis_count: int = 0
    completed_category_count: int | None = None


@dataclass(frozen=True, slots=True)
class TrendDiscoveryConflict:
    """同時実行した別のワーカーが先に保存したため、保存しなかった。"""

    window_end: date
    source_analysis_count: int
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
        window = TrendWindow.latest(datetime.now(UTC))
        try:
            outcome = await self.execute(window)
        except Exception as exc:
            await append_trend_discovery_run_event_best_effort(
                self._session_factory,
                event_type=EventType.FAILED,
                outcome_code=TrendDiscoveryOutcomeCode.RUN_FAILED,
                window_start=window.window_start,
                window_end=window.window_end,
                exc=exc,
            )
            raise

        if isinstance(outcome, SkippedAlreadyGenerated):
            logger.info(
                "trend_discovery_task_skipped_already_exists",
                window_end=outcome.window_end.isoformat(),
            )
            return
        if isinstance(outcome, SkippedNoTargetArticles):
            logger.info(
                "trend_discovery_task_skipped_no_target_articles",
                window_end=outcome.window_end.isoformat(),
            )
            return
        if isinstance(outcome, TrendDiscoveryConflict):
            logger.info(
                "trend_discovery_task_conflict",
                window_end=outcome.window_end.isoformat(),
                source_analysis_count=outcome.source_analysis_count,
                category_count=outcome.completed_category_count,
            )
            return

        await append_trend_discovery_run_event_best_effort(
            self._session_factory,
            event_type=EventType.SUCCEEDED,
            outcome_code=TrendDiscoveryOutcomeCode.RUN_COMPLETED,
            window_start=window.window_start,
            window_end=outcome.window_end,
            source_analysis_count=outcome.source_analysis_count,
            completed_category_count=outcome.completed_category_count,
        )

        logger.info(
            "trend_discovery_task_completed",
            window_end=outcome.window_end.isoformat(),
            source_analysis_count=outcome.source_analysis_count,
            category_count=outcome.completed_category_count,
        )
        await notifier.notify(tags=TRENDS_REVALIDATE_TAGS)

    async def execute(self, window: TrendWindow) -> TrendDiscoveryOutcome:
        """準備から保存までを同じセッションで扱い、開始条件を満たした期間を集計する。"""
        async with self._session_factory() as session:
            facts = await TrendsRepository(session).load_ready_build_facts(
                window=window
            )
            ready = ReadyForTrendDiscovery.from_facts(window=window, facts=facts)
            if ready is TrendDiscoveryReadyBuildRejectionReason.ALREADY_GENERATED:
                return SkippedAlreadyGenerated(window_end=window.window_end)
            if ready is TrendDiscoveryReadyBuildRejectionReason.NO_ARTICLES:
                return SkippedNoTargetArticles(window_end=window.window_end)
            outcome = await self._generate(session, ready)
            await session.commit()
            return outcome

    async def _generate(
        self, session: AsyncSession, ready: ReadyForTrendDiscovery
    ) -> TrendDiscoveryCompleted | TrendDiscoveryConflict:
        """開始条件を満たした期間を集計し、既存行を上書きせず保存する。"""
        window = ready.window
        trends_repo = TrendsRepository(session)
        categories = await trends_repo.get_categories()
        category_trends_list: list[CategoryTrends] = []
        for cat in categories:
            category_trends_list.append(
                await self._build_category_trends(
                    trends_repo,
                    category=cat,
                    current_start=window.current_start,
                    current_end=window.current_end,
                    previous_start=window.previous_start,
                )
            )
        category_trends = tuple(category_trends_list)
        completed_category_count = len(category_trends)
        bundle = TrendsBundle(window=window, category_trends=category_trends)
        generated_at = datetime.now(UTC)
        response = trends_from_snapshot(
            bundle=bundle,
            generated_at=generated_at,
            source_analysis_count=ready.source_analysis_count,
        )
        snapshot = TrendsSnapshot(
            window_end=window.window_end,
            bundle=response.model_dump(mode="json", by_alias=True),
            source_analysis_count=ready.source_analysis_count,
            generated_at=generated_at,
        )
        save_result = await SnapshotRepository(session).save(snapshot)
        if save_result.status == SnapshotSaveStatus.CONFLICT:
            return TrendDiscoveryConflict(
                window_end=window.window_end,
                source_analysis_count=ready.source_analysis_count,
                completed_category_count=completed_category_count,
            )
        return TrendDiscoveryCompleted(
            window_end=window.window_end,
            source_analysis_count=ready.source_analysis_count,
            completed_category_count=completed_category_count,
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
        """上位メンションを補足し、両ランキングで同じメンションの情報を共有する。"""
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
